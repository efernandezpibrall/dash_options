"""Preserve six-decimal option quantities in immutable valuation drafts.

Revision ID: 20260831_04
Revises: 20260831_03
Create Date: 2026-08-31
"""

import sqlalchemy as sa
from alembic import op


revision = "20260831_04"
down_revision = "20260831_03"
branch_labels = None
depends_on = None

SCHEMA = "at_lng"
TABLE = "trades_options_valuation"
COLUMN = "quantity"
CURRENT_VIEW = "trades_options_valuation_current"


def _dependent_view_snapshot(bind):
    row = bind.execute(
        sa.text(
            """
            SELECT pg_get_viewdef(c.oid, true) AS view_definition,
                   pg_get_userbyid(c.relowner) AS owner_name,
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
    owner_name = row["owner_name"]
    may_manage = bind.execute(
        sa.text("SELECT pg_has_role(current_user, :owner_name, 'USAGE')"),
        {"owner_name": owner_name},
    ).scalar_one()
    if not may_manage:
        raise RuntimeError(
            f"Revision 20260831_04 must run as owner {owner_name!r} or a "
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
            f"Revision 20260831_04 found dependent views that require an "
            f"explicit migration: {dependants}"
        )
    grants = bind.execute(
        sa.text(
            """
            SELECT grantee, privilege_type, is_grantable
            FROM information_schema.role_table_grants
            WHERE table_schema = :schema
              AND table_name = :view_name
            ORDER BY grantee, privilege_type
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


def upgrade() -> None:
    bind = op.get_bind()
    snapshot = _dependent_view_snapshot(bind)
    if snapshot is not None:
        op.execute(f'DROP VIEW "{SCHEMA}"."{CURRENT_VIEW}"')
    op.alter_column(
        TABLE,
        COLUMN,
        schema=SCHEMA,
        existing_type=sa.BigInteger(),
        type_=sa.Numeric(precision=30, scale=6),
        existing_nullable=True,
        postgresql_using=f"{COLUMN}::numeric(30,6)",
    )
    _restore_dependent_view(bind, snapshot)


def downgrade() -> None:
    raise RuntimeError(
        "Revision 20260831_04 preserves contractual fractional quantities and "
        "has no safe automatic downgrade after such drafts exist."
    )
