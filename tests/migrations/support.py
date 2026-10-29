"""Independent PostgreSQL catalog/data snapshots for baseline equivalence tests."""

import hashlib
import json
import re
from sqlalchemy import text


def js(query, connection):
    return [dict(row) for row in connection.execute(text(query)).mappings()]


def catalog(engine):
    with engine.connect() as c:
        return {
            "relations": js(
                "SELECT c.relname,c.relkind,c.relrowsecurity,c.relforcerowsecurity FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='at_lng' AND c.relkind IN ('r','p','v','m','S') ORDER BY 1",
                c,
            ),
            "columns": js(
                "SELECT c.relname,a.attname,format_type(a.atttypid,a.atttypmod) AS type,a.attnotnull,pg_get_expr(d.adbin,d.adrelid) AS default_expr,a.attidentity,a.attgenerated FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum WHERE n.nspname='at_lng' AND c.relkind IN ('r','p','v','m') AND a.attnum>0 AND NOT a.attisdropped ORDER BY c.relname,a.attnum",
                c,
            ),
            "constraints": js(
                "SELECT c.relname,k.conname,k.contype,k.convalidated,k.condeferrable,k.condeferred,pg_get_constraintdef(k.oid,true) AS definition FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='at_lng' ORDER BY c.relname,k.conname",
                c,
            ),
            "indexes": js(
                "SELECT tablename AS relname,indexname,indexdef FROM pg_indexes WHERE schemaname='at_lng' ORDER BY tablename,indexname",
                c,
            ),
            "triggers": js(
                "SELECT c.relname,t.tgname,t.tgenabled,pg_get_triggerdef(t.oid,true) AS definition FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='at_lng' AND NOT t.tgisinternal ORDER BY c.relname,t.tgname",
                c,
            ),
            "functions": js(
                "SELECT p.proname,p.prosecdef,p.provolatile,p.proconfig,pg_get_functiondef(p.oid) AS definition FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='at_lng' AND p.prokind='f' ORDER BY p.proname,pg_get_function_identity_arguments(p.oid)",
                c,
            ),
            "views": js(
                "SELECT c.relname,pg_get_viewdef(c.oid,true) AS definition FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='at_lng' AND c.relkind='v' ORDER BY 1",
                c,
            ),
        }


def digest(v):
    return hashlib.sha256(json.dumps(v, sort_keys=True, default=str).encode()).hexdigest()


def canonical_catalog(value):
    # pg_dump/reparse pushes varchar[] -> text[] casts down to each literal.
    # Normalize only arrays made entirely of string literals; keep other SQL exact.
    def normalize_string(s):
        pattern = r"ARRAY\[((?:'(?:[^']|'')*'::character varying(?:::text)?(?:, )?)+)\](?:::text\[\])?"

        def replace(match):
            body = re.sub(r"::character varying(?:::text)?", "::text", match.group(1))
            return "ARRAY[" + body + "]"

        return re.sub(pattern, replace, s)

    def walk(v):
        if isinstance(v, str):
            return normalize_string(v)
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        return v

    return walk(value)


def data_state(engine):
    with engine.connect() as c:
        tables = (
            c.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='at_lng' ORDER BY tablename"))
            .scalars()
            .all()
        )
        return {
            n: digest(
                sorted(
                    [dict(r) for r in c.execute(text("SELECT * FROM at_lng." + n)).mappings()],
                    key=lambda x: json.dumps(x, sort_keys=True, default=str),
                )
            )
            for n in tables
        }


def oids(engine):
    with engine.connect() as c:
        return dict(
            c.execute(
                text("SELECT relname,oid FROM pg_class WHERE relnamespace='at_lng'::regnamespace ORDER BY 1")
            ).all()
        )


def seed_sentinels(engine):
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO at_lng.vol_calibration_runs(run_id,commodity,cob_date,input_fingerprint,engine_version,configuration,created_by,idempotency_key,status) VALUES ('11111111-1111-1111-1111-111111111111','BRENT','2026-10-29','baseline-sentinel','rehearsal','{}','baseline-test','baseline-test-run','draft')"
            )
        )
        c.execute(
            text(
                "INSERT INTO at_lng.live_forward_curve_revisions(revision,scope,captured_at,created_at,created_by,payload,digest) VALUES ('baseline-sentinel','desk-test',now(),now(),'baseline-test','{}','baseline-sentinel')"
            )
        )
        c.execute(
            text(
                "INSERT INTO at_lng.live_forward_curve_heads(scope,revision,version) VALUES ('desk-test','baseline-sentinel',1)"
            )
        )
        c.execute(text("INSERT INTO at_lng.trades_options_valuation(quantity,model) VALUES (1.234567,'black76')"))
