"""Versioned Brent/HH policies, durable candidates and guarded promotion.

Revision ID: 20261005_01
Revises: 20260930_01
"""
from alembic import op
import sqlalchemy as sa

SCHEMA = "at_lng"
CURRENT_VIEW = "ttf_vol_surface_publication_current"

revision = "20261005_01"
down_revision = "20260930_01"
branch_labels = None
depends_on = None


def _dependent_view_snapshot(bind):
    row = bind.execute(
        sa.text(
            """
            SELECT pg_get_viewdef(c.oid, true) AS view_definition,
                   pg_get_userbyid(c.relowner) AS owner_name,
                   c.reloptions,
                   obj_description(c.oid, 'pg_class') AS comment
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = :schema
              AND c.relname = :view_name
              AND c.relkind = 'v'
            """
        ),
        {"schema": SCHEMA, "view_name": CURRENT_VIEW},
    ).mappings().first()
    if row is None:
        return None
    if row["reloptions"]:
        raise RuntimeError("Surface view options require an explicit preservation migration")
    column_acl = bind.execute(sa.text("SELECT EXISTS(SELECT 1 FROM pg_attribute WHERE attrelid=to_regclass(:view) AND attacl IS NOT NULL)"),
                              {"view": f"{SCHEMA}.{CURRENT_VIEW}"}).scalar_one()
    if column_acl:
        raise RuntimeError("Surface view column grants require an explicit preservation migration")
    owner_name = row["owner_name"]
    may_manage = bind.execute(
        sa.text("SELECT pg_has_role(current_user, :owner_name, 'USAGE')"),
        {"owner_name": owner_name},
    ).scalar_one()
    if not may_manage:
        raise RuntimeError(
            f"Revision 20261005_01 must run as owner {owner_name!r} or a "
            f"member of that role to preserve {SCHEMA}.{CURRENT_VIEW}"
        )
    dependants = bind.execute(
        sa.text(
            """
            SELECT dependent_ns.nspname, dependent_view.relname
            FROM pg_depend
            JOIN pg_rewrite ON pg_depend.objid = pg_rewrite.oid
            JOIN pg_class dependent_view
              ON pg_rewrite.ev_class = dependent_view.oid
            JOIN pg_namespace dependent_ns
              ON dependent_view.relnamespace = dependent_ns.oid
            WHERE pg_depend.refobjid = to_regclass(:qualified_view)
              AND dependent_view.relkind = 'v'
              AND dependent_view.oid <> to_regclass(:qualified_view)
            """
        ),
        {"qualified_view": f"{SCHEMA}.{CURRENT_VIEW}"},
    ).all()
    if dependants:
        raise RuntimeError(
            f"Revision 20261005_01 found dependent views that require an "
            f"explicit migration: {dependants}"
        )
    grants = bind.execute(
        sa.text(
            """
            SELECT CASE WHEN acl.grantee=0 THEN 'PUBLIC' ELSE pg_get_userbyid(acl.grantee) END AS grantee,
                   acl.privilege_type, CASE WHEN acl.is_grantable THEN 'YES' ELSE 'NO' END AS is_grantable
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace,
                 LATERAL aclexplode(COALESCE(c.relacl,acldefault('r',c.relowner))) acl
            WHERE n.nspname=:schema AND c.relname=:view_name
            ORDER BY grantee, acl.privilege_type
            """
        ),
        {"schema": SCHEMA, "view_name": CURRENT_VIEW},
    ).mappings().all()
    return {
        "definition": row["view_definition"],
        "owner": owner_name,
        "comment": row["comment"],
        "grants": [dict(grant) for grant in grants],
    }


def _restore_dependent_view(bind, snapshot):
    if snapshot is None:
        return
    preparer = bind.dialect.identifier_preparer
    qualified_view = (
        f"{preparer.quote(SCHEMA)}.{preparer.quote(CURRENT_VIEW)}"
    )
    bind.execute(
        sa.text(f"CREATE VIEW {qualified_view} AS {snapshot['definition']}")
    )
    defaults = bind.execute(sa.text("""SELECT DISTINCT CASE WHEN acl.grantee=0 THEN 'PUBLIC' ELSE pg_get_userbyid(acl.grantee) END
        FROM pg_class c, LATERAL aclexplode(COALESCE(c.relacl,acldefault('r',c.relowner))) acl
        WHERE c.oid=to_regclass(:view)"""), {"view": f"{SCHEMA}.{CURRENT_VIEW}"}).scalars().all()
    for grantee in defaults:
        bind.execute(sa.text(f"REVOKE ALL ON {qualified_view} FROM {'PUBLIC' if grantee == 'PUBLIC' else preparer.quote(grantee)}"))
    for grant in snapshot["grants"]:
        grantee = (
            "PUBLIC"
            if grant["grantee"] == "PUBLIC"
            else preparer.quote(grant["grantee"])
        )
        grant_option = (
            " WITH GRANT OPTION"
            if grant["is_grantable"] == "YES"
            else ""
        )
        bind.execute(
            sa.text(
                f"GRANT {grant['privilege_type']} ON {qualified_view} "
                f"TO {grantee}{grant_option}"
            )
        )
    if snapshot["comment"] is not None:
        escaped_comment = str(snapshot["comment"]).replace("'", "''")
        bind.execute(
            sa.text(
                f"COMMENT ON VIEW {qualified_view} IS '{escaped_comment}'"
            )
        )
    bind.execute(
        sa.text(
            f"ALTER VIEW {qualified_view} OWNER TO "
            f"{preparer.quote(snapshot['owner'])}"
        )
    )



def upgrade():
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout = '5s'")
    view = _dependent_view_snapshot(bind)
    if view is not None:
        op.execute(f'DROP VIEW "{SCHEMA}"."{CURRENT_VIEW}"')
    # Remove decimal scale truncation. PostgreSQL stores the shortest decimal
    # representation of each float without imposing ten decimal places.
    for column in ("strike", "delta", "volatility", "total_variance", "working_forward"):
        op.execute(f"ALTER TABLE at_lng.implied_volatility_surface_calibrated ALTER COLUMN {column} TYPE numeric")
    for column in ("weighted_rmse", "unweighted_rmse", "max_error"):
        op.execute(f"ALTER TABLE at_lng.vol_calibration_expiry_results ALTER COLUMN {column} TYPE numeric")
    _restore_dependent_view(bind, view)
    # Extend existing predicates verbatim; retain every historical policy.
    for name in ("ck_calibrated_surface_five_product_dense_policy", "ck_calibrated_surface_hh_source_policy"):
        op.execute(f"""DO $migration$ DECLARE predicate text; BEGIN
            SELECT pg_get_expr(conbin, conrelid) INTO STRICT predicate FROM pg_constraint
            WHERE conrelid='at_lng.implied_volatility_surface_calibrated'::regclass AND conname='{name}';
            predicate := replace(predicate, '= ''brent_single_svi_actual_strikes_v1''::text',
                '= ANY (ARRAY[''brent_single_svi_actual_strikes_v1''::text, ''brent_single_svi_actual_strikes_v2''::text])');
            predicate := replace(predicate, '= ''hh_lne_single_svi_seasonal_quotes_v1''::text',
                '= ANY (ARRAY[''hh_lne_single_svi_seasonal_quotes_v1''::text, ''hh_lne_single_svi_seasonal_quotes_v2''::text])');
            IF strpos(predicate, 'hh_lne_single_svi_seasonal_quotes_v2') = 0 THEN
                RAISE EXCEPTION 'Policy predicate was not extended: {name}';
            END IF;
            EXECUTE 'ALTER TABLE at_lng.implied_volatility_surface_calibrated DROP CONSTRAINT {name}';
            EXECUTE 'ALTER TABLE at_lng.implied_volatility_surface_calibrated ADD CONSTRAINT {name} CHECK (' || predicate || ') NOT VALID';
        END $migration$""")
    op.execute("""CREATE TABLE at_lng.vol_settlement_candidates (
        job_id uuid PRIMARY KEY REFERENCES at_lng.vol_calibration_jobs(job_id),
        commodity text NOT NULL CHECK (commodity IN ('BRENT','HH')),
        cob_date date NOT NULL, input_identity text NOT NULL,
        state text NOT NULL CHECK (state IN ('prepared','shadow','published','held')),
        candidate jsonb, diagnostics jsonb NOT NULL DEFAULT '{}'::jsonb,
        publication_id uuid REFERENCES at_lng.vol_surface_publications(publication_id),
        updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    op.execute("CREATE INDEX ON at_lng.vol_settlement_candidates (commodity,cob_date,state)")
    op.execute("""CREATE TABLE at_lng.vol_settlement_source_events (
        event_identity text PRIMARY KEY, commodity text NOT NULL,
        cob_date date NOT NULL, snapshot_id uuid,
        state text NOT NULL CHECK (state IN ('waiting','queued','held')),
        reason text, job_id uuid REFERENCES at_lng.vol_calibration_jobs(job_id),
        updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    op.execute("""CREATE TABLE at_lng.vol_settlement_promotion (
        commodity text PRIMARY KEY CHECK (commodity IN ('BRENT','HH')),
        shadow_start_cob date NOT NULL DEFAULT CURRENT_DATE,
        required_sessions integer NOT NULL DEFAULT 20 CHECK (required_sessions >= 20),
        approved boolean NOT NULL DEFAULT false,
        approved_by text, approved_at timestamptz,
        CHECK (NOT approved OR (approved_by IS NOT NULL AND approved_at IS NOT NULL))
    )""")
    op.execute("INSERT INTO at_lng.vol_settlement_promotion (commodity) VALUES ('BRENT'),('HH')")


def downgrade():
    raise RuntimeError("Published v2 surfaces and audit evidence require a reviewed rollback")
