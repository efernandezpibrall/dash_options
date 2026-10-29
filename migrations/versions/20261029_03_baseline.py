"""Frozen final dashboard schema at 20261029_03.

Existing databases at this revision perform no DDL or data writes. Fresh
installations require the externally owned trade/catalogue schema. Older
checkpoints must first use scripts.maintenance.upgrade_legacy_schema.

Derived from the full b946433 migration replay on PostgreSQL 17. The archived
chain independently verifies schema and seed parity. Do not import mutable
application metadata or reintroduce retired history tables here.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects import postgresql


SCHEMA = "at_lng"

VALUATION_TABLE = "trades_options_valuation"

COMPONENTS_CONSTRAINT = "trades_options_valuation_adjustment_components_ck"


def _leg_component_constraint(suffix: str) -> str:
    components = f"volatility_adjustment_components_{suffix}"
    maturity_type = f"maturity_date_type_{suffix}"
    scalar_columns = (
        f"surface_expiry_date_{suffix}",
        f"surface_expiry_status_{suffix}",
        f"volatility_reference_convention_{suffix}",
        f"volatility_reference_convention_version_{suffix}",
        f"variance_calendar_code_{suffix}",
        f"variance_calendar_version_{suffix}",
        f"business_days_to_trade_expiry_{suffix}",
        f"business_days_to_surface_expiry_{suffix}",
        f"vol_adjustment_factor_{suffix}",
    )
    scalar_nulls = " AND ".join(f"{column} IS NULL" for column in scalar_columns)
    return f"""
    CASE
        WHEN {components} IS NULL THEN true
        WHEN jsonb_typeof({components}) <> 'array' THEN false
        ELSE (
            (
                (lower(trim({maturity_type})) = 'calendar'
                 AND jsonb_array_length({components}) = 12)
                OR
                (lower(trim({maturity_type})) = 'brent301'
                 AND asset_{suffix} = 'ICE_BRENT_FUTURES'
                 AND jsonb_array_length({components}) = 3)
            )
            AND {scalar_nulls}
        )
    END
    """


def _prepare_brent_components() -> None:
    op.drop_constraint(
        COMPONENTS_CONSTRAINT,
        VALUATION_TABLE,
        schema=SCHEMA,
        type_="check",
    )
    op.create_check_constraint(
        COMPONENTS_CONSTRAINT,
        VALUATION_TABLE,
        f"({_leg_component_constraint('a')}) AND ({_leg_component_constraint('b')})",
        schema=SCHEMA,
        postgresql_not_valid=True,
    )
    op.execute(f"ALTER TABLE {SCHEMA}.{VALUATION_TABLE} VALIDATE CONSTRAINT {COMPONENTS_CONSTRAINT}")


QUANTITY_COLUMN = "quantity"

VALUATION_VIEW = "trades_options_valuation_current"


def _valuation_view_snapshot(bind):
    row = (
        bind.execute(
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
            {"schema": SCHEMA, "view_name": VALUATION_VIEW},
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    owner_name = row["owner_name"]
    may_manage = bind.execute(
        sa.text("SELECT pg_has_role(current_user, :owner_name, 'USAGE')"),
        {"owner_name": owner_name},
    ).scalar_one()
    if not may_manage:
        raise RuntimeError(
            f"Baseline 20261029_03 must run as owner {owner_name!r} or a "
            f"member of that role to preserve {SCHEMA}.{VALUATION_VIEW}"
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
        {"qualified_view": f"{SCHEMA}.{VALUATION_VIEW}"},
    ).all()
    if dependants:
        raise RuntimeError(
            f"Baseline 20261029_03 found dependent views that require an explicit migration: {dependants}"
        )
    grants = (
        bind.execute(
            sa.text(
                """
            SELECT grantee, privilege_type, is_grantable
            FROM information_schema.role_table_grants
            WHERE table_schema = :schema
              AND table_name = :view_name
            ORDER BY grantee, privilege_type
            """
            ),
            {"schema": SCHEMA, "view_name": VALUATION_VIEW},
        )
        .mappings()
        .all()
    )
    return {
        "definition": row["view_definition"],
        "owner": owner_name,
        "comment": row["comment"],
        "grants": [dict(grant) for grant in grants],
    }


def _restore_valuation_view(bind, snapshot):
    if snapshot is None:
        return
    preparer = bind.dialect.identifier_preparer
    qualified_view = f"{preparer.quote(SCHEMA)}.{preparer.quote(VALUATION_VIEW)}"
    bind.execute(sa.text(f"CREATE VIEW {qualified_view} AS {snapshot['definition']}"))
    for grant in snapshot["grants"]:
        grantee = "PUBLIC" if grant["grantee"] == "PUBLIC" else preparer.quote(grant["grantee"])
        grant_option = " WITH GRANT OPTION" if grant["is_grantable"] == "YES" else ""
        bind.execute(sa.text(f"GRANT {grant['privilege_type']} ON {qualified_view} TO {grantee}{grant_option}"))
    if snapshot["comment"] is not None:
        escaped_comment = str(snapshot["comment"]).replace("'", "''")
        bind.execute(sa.text(f"COMMENT ON VIEW {qualified_view} IS '{escaped_comment}'"))
    bind.execute(sa.text(f"ALTER VIEW {qualified_view} OWNER TO {preparer.quote(snapshot['owner'])}"))


def _preserve_fractional_quantities() -> None:
    bind = op.get_bind()
    snapshot = _valuation_view_snapshot(bind)
    if snapshot is not None:
        op.execute(f'DROP VIEW "{SCHEMA}"."{VALUATION_VIEW}"')
    op.alter_column(
        VALUATION_TABLE,
        QUANTITY_COLUMN,
        schema=SCHEMA,
        existing_type=sa.BigInteger(),
        type_=sa.Numeric(precision=30, scale=6),
        existing_nullable=True,
        postgresql_using=f"{QUANTITY_COLUMN}::numeric(30,6)",
    )
    _restore_valuation_view(bind, snapshot)


CONVENTIONS = (
    (
        "Q2K-B301-MINUS1-M-MPLUS1-V1",
        "OTC",
        "BRENT301",
        "kirk",
        "european",
        "futures_style",
        "physical_delivery",
        "USD",
        False,
        "internal_contract_terms",
        None,
        None,
    ),
    (
        "ICE_BRENT_AMERICAN_218",
        "ICE",
        "B",
        "american_futures",
        "american",
        "futures_style",
        "futures_delivery",
        "USD",
        False,
        "https://www.ice.com/products/218/Brent-Crude-American-style-Option",
        "black76",
        None,
    ),
    (
        "CME_BRENT_BZO_504",
        "CME",
        "BZO",
        "american_futures",
        "american",
        "futures_style",
        "futures_delivery",
        "USD",
        False,
        "https://www.cmegroup.com/rulebook/NYMEX/5/504.pdf",
        "black76",
        None,
    ),
    (
        "CME_BRENT_BE_378",
        "CME",
        "BE",
        "black76",
        "european",
        "equity_style",
        "cash",
        "USD",
        True,
        "https://www.cmegroup.com/rulebook/NYMEX/3/378.pdf",
        None,
        None,
    ),
    (
        "ICE_TTF_TFO",
        "ICE",
        "TFO",
        "black76",
        "european",
        "futures_style",
        "futures_delivery",
        "EUR",
        False,
        "https://www.ice.com/products/71085679",
        None,
        None,
    ),
    (
        "ICE_JKM_71090519",
        "ICE",
        "JKM",
        "asian76",
        "european",
        "futures_style",
        "futures_delivery",
        "USD",
        False,
        "https://www.ice.com/products/71090519/JKM-LNG-Platts-Average-Price-Options",
        None,
        "JKM_CONTINUOUS_16_TO_EXPIRY_V1",
    ),
    (
        "ICE_JKM_JKZ",
        "ICE",
        "JKZ",
        "black76",
        "european",
        "futures_style",
        "futures_delivery",
        "USD",
        False,
        "https://www.ice.com/publicdocs/circulars/26019.pdf",
        None,
        None,
    ),
    (
        "ICE_NBP_UKF_71085728",
        "ICE",
        "UKF",
        "black76",
        "european",
        "futures_style",
        "futures_delivery",
        "GBP",
        False,
        "https://www.ice.com/products/71085728/UK-NBP-Natural-Gas-Options-Futures-Style-Margin",
        None,
        None,
    ),
    (
        "ICE_HH_PHE_6590274",
        "ICE",
        "PHE",
        "black76",
        "european",
        "equity_style",
        "futures_delivery",
        "USD",
        True,
        "https://www.ice.com/products/6590274/Option-on-Henry-Penultimate-Fixed-Price-Future",
        None,
        None,
    ),
    (
        "CME_HH_LNE_560",
        "CME",
        "LNE",
        "black76",
        "european",
        "equity_style",
        "cash",
        "USD",
        True,
        "https://www.cmegroup.com/rulebook/NYMEX/5/560.pdf",
        None,
        None,
    ),
    (
        "CME_TTF_TTO_1161",
        "CME",
        "TTO",
        "black76",
        "european",
        "equity_style",
        "futures_delivery",
        "EUR",
        True,
        "https://www.cmegroup.com/rulebook/NYMEX/11/1161.pdf",
        None,
        None,
    ),
    (
        "CME_TTF_TFO_1162",
        "CME",
        "TFO",
        "black76",
        "european",
        "futures_style",
        "futures_delivery",
        "EUR",
        False,
        "https://www.cmegroup.com/rulebook/NYMEX/11/1162.pdf",
        None,
        None,
    ),
    (
        "CME_TTF_TTL_1016",
        "CME",
        "TTL",
        "black76",
        "european",
        "futures_style",
        "cash",
        "EUR",
        False,
        "https://www.cmegroup.com/rulebook/NYMEX/10/1016.pdf",
        None,
        None,
    ),
    (
        "CME_TTF_TFP_1018",
        "CME",
        "TFP",
        "black76",
        "european",
        "equity_style",
        "cash",
        "USD",
        True,
        "https://www.cmegroup.com/rulebook/NYMEX/10/1018.pdf",
        None,
        None,
    ),
    (
        "CME_TTF_TFF_1019",
        "CME",
        "TFF",
        "black76",
        "european",
        "futures_style",
        "cash",
        "USD",
        False,
        "https://www.cmegroup.com/rulebook/NYMEX/10/1019.pdf",
        None,
        None,
    ),
    (
        "CME_JKM_JKO_869",
        "CME",
        "JKO",
        "asian76",
        "european",
        "equity_style",
        "cash",
        "USD",
        True,
        "https://www.cmegroup.com/rulebook/NYMEX/8/869.pdf",
        None,
        "JKM_PLATTS_PUBLICATION_DAYS_16_TO_15_V1",
    ),
    (
        "CME_JKM_JFO_864",
        "CME",
        "JFO",
        "asian76",
        "european",
        "futures_style",
        "cash",
        "USD",
        False,
        "https://www.cmegroup.com/rulebook/NYMEX/8/864.pdf",
        None,
        "JKM_PLATTS_PUBLICATION_DAYS_16_TO_15_V1",
    ),
    (
        "CME_HH_ON_370",
        "CME",
        "ON",
        "american_futures",
        "american",
        "equity_style",
        "futures_delivery",
        "USD",
        True,
        "https://www.cmegroup.com/rulebook/NYMEX/3/370.pdf",
        None,
        None,
    ),
    (
        "CME_NBP_UKO_1163",
        "CME",
        "UKO",
        "black76",
        "european",
        "equity_style",
        "futures_delivery",
        "GBP",
        True,
        "https://www.cmegroup.com/rulebook/NYMEX/11/1163.pdf",
        None,
        None,
    ),
    (
        "CME_NBP_UFO_1164",
        "CME",
        "UFO",
        "black76",
        "european",
        "futures_style",
        "futures_delivery",
        "GBP",
        False,
        "https://www.cmegroup.com/rulebook/NYMEX/11/1164.pdf",
        None,
        None,
    ),
    (
        "CME_WTI_LO",
        "CME",
        "LO",
        "american_futures",
        "american",
        "equity_style",
        "futures_delivery",
        "USD",
        True,
        "https://www.cmegroup.com/rulebook/NYMEX/3/310.pdf",
        None,
        None,
    ),
    (
        "CME_WTI_WEEKLY",
        "CME",
        "WTI_WEEKLY",
        "american_futures",
        "american",
        "equity_style",
        "futures_delivery",
        "USD",
        True,
        "https://www.cmegroup.com/rulebook/NYMEX/10/1011.pdf",
        None,
        None,
    ),
    (
        "OTC_AMERICAN_FUTURES_EQUITY",
        "OTC",
        "OTC_AMERICAN_FUTURES",
        "american_futures",
        "american",
        "equity_style",
        "futures_delivery",
        "CONTRACT_CURRENCY",
        True,
        "internal_contract_terms",
        None,
        None,
    ),
    (
        "OTC_AMERICAN_FUTURES_STYLE",
        "OTC",
        "OTC_AMERICAN_FUTURES",
        "american_futures",
        "american",
        "futures_style",
        "futures_delivery",
        "CONTRACT_CURRENCY",
        False,
        "internal_contract_terms",
        None,
        None,
    ),
)


def _prepare_option_families() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "option_contract_conventions",
        sa.Column("pricer_pricing_model", sa.Text(), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "option_contract_conventions",
        sa.Column("asian_averaging_rule_code", sa.Text(), nullable=True),
        schema=SCHEMA,
    )

    statement = sa.text(
        f"""
        INSERT INTO {SCHEMA}.option_contract_conventions (
            convention_code, venue, product_code, pricing_model,
            exercise_style, margin_style, settlement_type, premium_currency,
            discount_curve_required, source_url, pricer_pricing_model,
            asian_averaging_rule_code, reviewed_at, active
        ) VALUES (
            :convention_code, :venue, :product_code, :pricing_model,
            :exercise_style, :margin_style, :settlement_type, :premium_currency,
            :discount_curve_required, :source_url, :pricer_pricing_model,
            :asian_averaging_rule_code, now(), true
        )
        ON CONFLICT (convention_code) DO UPDATE SET
            venue = EXCLUDED.venue,
            product_code = EXCLUDED.product_code,
            pricing_model = EXCLUDED.pricing_model,
            exercise_style = EXCLUDED.exercise_style,
            margin_style = EXCLUDED.margin_style,
            settlement_type = EXCLUDED.settlement_type,
            premium_currency = EXCLUDED.premium_currency,
            discount_curve_required = EXCLUDED.discount_curve_required,
            source_url = EXCLUDED.source_url,
            pricer_pricing_model = EXCLUDED.pricer_pricing_model,
            asian_averaging_rule_code = EXCLUDED.asian_averaging_rule_code,
            reviewed_at = now(),
            active = true
        """
    )
    bind = op.get_bind()
    columns = (
        "convention_code",
        "venue",
        "product_code",
        "pricing_model",
        "exercise_style",
        "margin_style",
        "settlement_type",
        "premium_currency",
        "discount_curve_required",
        "source_url",
        "pricer_pricing_model",
        "asian_averaging_rule_code",
    )
    for row in CONVENTIONS:
        bind.execute(statement, dict(zip(columns, row)))

    op.create_check_constraint(
        "option_contract_conventions_asian_rule_ck",
        "option_contract_conventions",
        "(pricing_model = 'asian76' AND asian_averaging_rule_code IS NOT NULL) "
        "OR (pricing_model <> 'asian76' AND asian_averaging_rule_code IS NULL)",
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "option_contract_conventions_approved_pricer_ck",
        "option_contract_conventions",
        "pricer_pricing_model IS NULL OR "
        "(pricing_model = 'american_futures' AND exercise_style = 'american' "
        "AND pricer_pricing_model = 'black76')",
        schema=SCHEMA,
    )

    op.drop_constraint(
        "trades_options_supported_convention_ck",
        "trades_options",
        schema=SCHEMA,
        type_="check",
    )
    op.create_check_constraint(
        "trades_options_supported_convention_ck",
        "trades_options",
        "lower(replace(replace(coalesce(model, ''), '_', ''), '-', '')) "
        "NOT IN ('black76', 'asian76', 'americanfutures') "
        "OR contract_convention_code IS NOT NULL",
        schema=SCHEMA,
    )

    op.add_column(
        "trades_options_valuation",
        sa.Column("pricing_model_application", sa.Text(), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "trades_options_valuation",
        sa.Column("model_horizon_date", sa.Date(), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "trades_options_valuation",
        sa.Column(
            "discount_factor_to_model_horizon",
            sa.Numeric(precision=30, scale=15),
            nullable=True,
        ),
        schema=SCHEMA,
    )
    op.add_column(
        "trades_options_valuation",
        sa.Column("asian_averaging_schedule", postgresql.JSONB(), nullable=True),
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "trades_options_valuation_model_application_ck",
        "trades_options_valuation",
        "pricing_model_application IS NULL OR pricing_model_application IN ('contractual', 'approved_approximation')",
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "trades_options_valuation_model_horizon_discount_ck",
        "trades_options_valuation",
        "discount_factor_to_model_horizon IS NULL OR "
        "(discount_factor_to_model_horizon <> 'NaN'::numeric AND "
        "discount_factor_to_model_horizon > 0)",
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "trades_options_valuation_asian_schedule_ck",
        "trades_options_valuation",
        "lower(replace(replace(coalesce(model, ''), '_', ''), '-', '')) "
        "<> 'asian76' OR (asian_averaging_schedule IS NOT NULL AND "
        "jsonb_typeof(asian_averaging_schedule) = 'object' AND "
        "model_horizon_date IS NOT NULL AND "
        "pricing_model_application = 'contractual')",
        schema=SCHEMA,
    )


revision = "20261029_03"

down_revision = None

branch_labels = None

depends_on = None

supersedes_revisions = (
    "20261021_01",
    "20261016_03",
    "20261016_02",
    "20261016_01",
    "20261029_01",
    "20261029_02",
    "20261028_01",
    "20261023_01",
    "20261012_01",
    "20261009_02",
    "20261009_01",
    "20261005_02",
    "20261005_01",
    "20260930_01",
    "20260929_03",
    "20260929_02",
    "20260929_01",
    "20260928_01",
    "20260902_02",
    "20260902_01",
    "20260831_04",
    "20260831_03",
    "20260831_02",
    "20260806_01",
    "20260803_01",
    "20260727_01",
)

_FINAL_SCHEMA_SQL = r"""
--
-- PostgreSQL database dump
--




--
-- Name: at_lng; Type: SCHEMA; Schema: -; Owner: -
--



--
-- Name: guard_brent_intraday_settlement(); Type: FUNCTION; Schema: at_lng; Owner: -
--

CREATE FUNCTION at_lng.guard_brent_intraday_settlement() RETURNS trigger
    LANGUAGE plpgsql
    AS $$ BEGIN
        IF NEW.commodity='BRENT' AND NEW.is_active AND EXISTS (
            SELECT 1 FROM at_lng.vol_calibration_runs r WHERE r.run_id=NEW.run_id
            AND (r.configuration->>'activation_scope'='intraday'
                OR r.configuration->'input_manifest'->>'version'='brent-intraday-svi-v1'
                OR EXISTS (SELECT 1 FROM jsonb_array_elements(
                    COALESCE(r.configuration->'input_manifest'->'source_snapshots','[]'::jsonb)) s
                    WHERE s->>'kind'='INTRADAY'))) THEN
            RAISE EXCEPTION 'Brent intraday evidence cannot activate settlement';
        END IF; RETURN NEW; END $$;


--
-- Name: guard_intraday_activation(); Type: FUNCTION; Schema: at_lng; Owner: -
--

CREATE FUNCTION at_lng.guard_intraday_activation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$ BEGIN
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
        END IF; RETURN NEW; END $$;


--
-- Name: guard_ttf_intraday_trade_mutation(); Type: FUNCTION; Schema: at_lng; Owner: -
--

CREATE FUNCTION at_lng.guard_ttf_intraday_trade_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
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


--
-- Name: guard_vol_calibration_run_mutation(); Type: FUNCTION; Schema: at_lng; Owner: -
--

CREATE FUNCTION at_lng.guard_vol_calibration_run_mutation() RETURNS trigger
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
        $$;


--
-- Name: guard_vol_surface_publication_mutation(); Type: FUNCTION; Schema: at_lng; Owner: -
--

CREATE FUNCTION at_lng.guard_vol_surface_publication_mutation() RETURNS trigger
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

            IF NEW.is_active AND EXISTS (SELECT 1 FROM at_lng.vol_calibration_runs r
                WHERE r.run_id=NEW.run_id AND r.configuration->>'activation_scope'='intraday') THEN
                RAISE EXCEPTION 'intraday revisions cannot activate the settlement lane';
            END IF;
            IF NEW.status = 'published' AND NOT NEW.is_active AND NOT EXISTS (
                SELECT 1 FROM at_lng.vol_calibration_runs r JOIN at_lng.vol_intraday_drafts d
                ON d.draft_id::text=r.configuration->'input_manifest'->>'draft_id'
                WHERE r.run_id=NEW.run_id AND NEW.commodity IN ('TTF','BRENT') AND d.commodity=NEW.commodity
                AND r.configuration->>'activation_scope'='intraday') THEN
                RAISE EXCEPTION 'published surfaces must be active';
            END IF;
            IF NEW.status IN ('superseded', 'rejected') AND NEW.is_active THEN
                RAISE EXCEPTION 'inactive publication status cannot remain active';
            END IF;

            RETURN NEW;
        END;
        $$;


--
-- Name: protect_publication_stage(); Type: FUNCTION; Schema: at_lng; Owner: -
--

CREATE FUNCTION at_lng.protect_publication_stage() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF OLD.ready AND NOT NEW.ready THEN RAISE EXCEPTION 'sealed publication stages cannot be reopened'; END IF;
            IF (to_jsonb(NEW)-'ready'-'updated_at') IS DISTINCT FROM
               (to_jsonb(OLD)-'ready'-'updated_at') THEN
                RAISE EXCEPTION 'publication staging evidence is immutable';
            END IF;
            RETURN NEW;
        END $$;


--
-- Name: protect_staged_point(); Type: FUNCTION; Schema: at_lng; Owner: -
--

CREATE FUNCTION at_lng.protect_staged_point() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
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
        END $$;


--
-- Name: reject_daily_artifact_mutation(); Type: FUNCTION; Schema: at_lng; Owner: -
--

CREATE FUNCTION at_lng.reject_daily_artifact_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN RAISE EXCEPTION 'daily artifacts are immutable'; END $$;


--
-- Name: reject_intraday_draft_mutation(); Type: FUNCTION; Schema: at_lng; Owner: -
--

CREATE FUNCTION at_lng.reject_intraday_draft_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$ BEGIN RAISE EXCEPTION 'intraday drafts are immutable'; END $$;


--
-- Name: reject_vol_calibration_mutation(); Type: FUNCTION; Schema: at_lng; Owner: -
--

CREATE FUNCTION at_lng.reject_vol_calibration_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
        END;
        $$;


--
-- Name: reject_vol_market_snapshot_mutation(); Type: FUNCTION; Schema: at_lng; Owner: -
--

CREATE FUNCTION at_lng.reject_vol_market_snapshot_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            RAISE EXCEPTION 'volatility market snapshots are append-only';
        END;
        $$;




--
-- Name: implied_volatility_surface_calibrated; Type: TABLE; Schema: at_lng; Owner: -
--

CREATE TABLE at_lng.implied_volatility_surface_calibrated (
    surface_point_id uuid DEFAULT gen_random_uuid() NOT NULL,
    publication_id uuid NOT NULL,
    run_id uuid NOT NULL,
    commodity character varying(16) NOT NULL,
    cob_date date NOT NULL,
    contract_date date NOT NULL,
    option_expiration_date date NOT NULL,
    strike numeric,
    delta numeric,
    put_call character varying(4) NOT NULL,
    volatility numeric NOT NULL,
    source_name character varying(255) NOT NULL,
    input_fingerprint character varying(128) NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    total_variance numeric,
    working_forward numeric,
    surface_region character varying(16),
    blend_classification character varying(32),
    calibration_basis character varying(16),
    calibration_method character varying(128),
    calibration_policy_version character varying(128),
    CONSTRAINT ck_calibrated_surface_coordinate CHECK (((strike IS NOT NULL) OR (delta IS NOT NULL)))
);


--
-- Name: live_forward_curve_activations; Type: TABLE; Schema: at_lng; Owner: -
--

CREATE TABLE at_lng.live_forward_curve_activations (
    scope character varying(100) NOT NULL,
    version integer NOT NULL,
    revision character varying(64) NOT NULL,
    previous_revision character varying(64),
    activated_at timestamp with time zone NOT NULL,
    activated_by character varying(200) NOT NULL,
    mode character varying(20) NOT NULL
);


--
-- Name: live_forward_curve_heads; Type: TABLE; Schema: at_lng; Owner: -
--

CREATE TABLE at_lng.live_forward_curve_heads (
    scope character varying(100) NOT NULL,
    revision character varying(64),
    version integer NOT NULL
);


--
-- Name: live_forward_curve_revisions; Type: TABLE; Schema: at_lng; Owner: -
--

CREATE TABLE at_lng.live_forward_curve_revisions (
    revision character varying(64) NOT NULL,
    scope character varying(100) NOT NULL,
    captured_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone NOT NULL,
    created_by character varying(200) NOT NULL,
    payload text NOT NULL,
    digest character varying(64) NOT NULL
);


--
-- Name: option_expiry_calendar; Type: TABLE; Schema: at_lng; Owner: -
--

CREATE TABLE at_lng.option_expiry_calendar (
    calendar_id uuid DEFAULT gen_random_uuid() NOT NULL,
    commodity character varying(16) NOT NULL,
    contract_date date NOT NULL,
    option_expiration_date date NOT NULL,
    exchange character varying(64) NOT NULL,
    source_name character varying(255) NOT NULL,
    source_reference text,
    verified_by character varying(255) NOT NULL,
    verified_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: vol_surface_publications; Type: TABLE; Schema: at_lng; Owner: -
--

CREATE TABLE at_lng.vol_surface_publications (
    publication_id uuid DEFAULT gen_random_uuid() NOT NULL,
    run_id uuid NOT NULL,
    commodity character varying(16) NOT NULL,
    cob_date date NOT NULL,
    status character varying(16) NOT NULL,
    is_active boolean DEFAULT false NOT NULL,
    approved_by character varying(255),
    approved_at timestamp with time zone,
    published_by character varying(255),
    published_at timestamp with time zone,
    supersedes_publication_id uuid,
    idempotency_key character varying(255) NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT ck_vol_surface_publications_lifecycle_fields CHECK (((((status)::text <> 'published'::text) OR ((is_active OR (((commodity)::text = 'TTF'::text) AND ((idempotency_key)::text ~~ 'tfo-intraday:%'::text)) OR (((commodity)::text = 'BRENT'::text) AND ((idempotency_key)::text ~~ 'brent-intraday:%'::text))) AND (published_by IS NOT NULL) AND (published_at IS NOT NULL))) AND (((status)::text <> ALL ((ARRAY['superseded'::character varying, 'rejected'::character varying])::text[])) OR (NOT is_active)))),
    CONSTRAINT ck_vol_surface_publications_status CHECK (((status)::text = ANY ((ARRAY['approved'::character varying, 'published'::character varying, 'superseded'::character varying, 'rejected'::character varying])::text[])))
);


--
-- Name: ttf_vol_surface_publication_current; Type: VIEW; Schema: at_lng; Owner: -
--

CREATE VIEW at_lng.ttf_vol_surface_publication_current AS
 SELECT p.publication_id,
    p.run_id,
    p.cob_date,
    p.published_at,
    p.published_by,
    s.contract_date,
    s.option_expiration_date,
    s.strike,
    s.delta,
    s.put_call,
    s.volatility,
    s.total_variance,
    s.working_forward,
    s.surface_region,
    s.blend_classification,
    s.calibration_basis,
    s.source_name,
    s.calibration_method,
    s.calibration_policy_version,
    s.input_fingerprint
   FROM (at_lng.vol_surface_publications p
     JOIN at_lng.implied_volatility_surface_calibrated s ON ((s.publication_id = p.publication_id)))
  WHERE (((p.commodity)::text = 'TTF'::text) AND ((p.status)::text = 'published'::text) AND p.is_active);


--
-- Name: vol_calibration_audit_events; Type: TABLE; Schema: at_lng; Owner: -
--

CREATE TABLE at_lng.vol_calibration_audit_events (
    event_id uuid DEFAULT gen_random_uuid() NOT NULL,
    run_id uuid NOT NULL,
    event_type character varying(64) NOT NULL,
    from_status character varying(16),
    to_status character varying(16),
    actor character varying(255) NOT NULL,
    details jsonb NOT NULL,
    event_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: vol_calibration_expiry_results; Type: TABLE; Schema: at_lng; Owner: -
--

CREATE TABLE at_lng.vol_calibration_expiry_results (
    result_id uuid DEFAULT gen_random_uuid() NOT NULL,
    run_id uuid NOT NULL,
    option_expiration_date date NOT NULL,
    parameters jsonb NOT NULL,
    diagnostics jsonb NOT NULL,
    validation jsonb NOT NULL,
    weighted_rmse numeric,
    unweighted_rmse numeric,
    max_error numeric,
    optimizer_success boolean NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: vol_calibration_intraday_trades; Type: TABLE; Schema: at_lng; Owner: -
--

CREATE TABLE at_lng.vol_calibration_intraday_trades (
    trade_id uuid DEFAULT gen_random_uuid() NOT NULL,
    commodity character varying(16) NOT NULL,
    business_date date NOT NULL,
    observed_at timestamp with time zone NOT NULL,
    contract_date date NOT NULL,
    option_expiration_date date NOT NULL,
    put_call character varying(1) NOT NULL,
    strike numeric(24,10) NOT NULL,
    mark_price numeric(24,10),
    mark_iv numeric(18,10) NOT NULL,
    volume numeric(24,6),
    forward numeric(24,10) NOT NULL,
    forward_observed_at timestamp with time zone NOT NULL,
    dte numeric(14,6) NOT NULL,
    call_delta numeric(12,10) NOT NULL,
    log_moneyness numeric(18,10) NOT NULL,
    day_count character varying(32) NOT NULL,
    delta_convention character varying(64) NOT NULL,
    pricing_model character varying(64) NOT NULL,
    method character varying(64) NOT NULL,
    entered_by character varying(255) NOT NULL,
    status character varying(16) DEFAULT 'active'::character varying NOT NULL,
    supersedes_trade_id uuid,
    notes text,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT ck_ttf_intraday_commodity CHECK (((commodity)::text = 'TTF'::text)),
    CONSTRAINT ck_ttf_intraday_delta CHECK (((call_delta > (0)::numeric) AND (call_delta < (1)::numeric))),
    CONSTRAINT ck_ttf_intraday_positive_values CHECK (((strike > (0)::numeric) AND (mark_iv > (0)::numeric) AND (mark_iv < (2)::numeric) AND (forward > (0)::numeric) AND (dte > (0)::numeric))),
    CONSTRAINT ck_ttf_intraday_put_call CHECK (((put_call)::text = ANY ((ARRAY['C'::character varying, 'P'::character varying])::text[]))),
    CONSTRAINT ck_ttf_intraday_status CHECK (((status)::text = ANY ((ARRAY['active'::character varying, 'superseded'::character varying])::text[])))
);


--
-- Name: vol_calibration_job_items; Type: TABLE; Schema: at_lng; Owner: -
--

CREATE TABLE at_lng.vol_calibration_job_items (
    item_id uuid DEFAULT gen_random_uuid() NOT NULL,
    job_id uuid NOT NULL,
    option_expiration_date date NOT NULL,
    status character varying(16) DEFAULT 'queued'::character varying NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    result_id uuid,
    last_error text,
    started_at timestamp with time zone,
    finished_at timestamp with time zone,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    input_fingerprint character varying(64),
    dependency_fingerprint character varying(64),
    result_payload jsonb,
    CONSTRAINT ck_vol_calibration_job_items_attempts CHECK ((attempts >= 0)),
    CONSTRAINT ck_vol_calibration_job_items_status CHECK (((status)::text = ANY ((ARRAY['queued'::character varying, 'running'::character varying, 'succeeded'::character varying, 'failed'::character varying, 'cancelled'::character varying, 'skipped'::character varying])::text[])))
);


--
-- Name: vol_calibration_jobs; Type: TABLE; Schema: at_lng; Owner: -
--

CREATE TABLE at_lng.vol_calibration_jobs (
    job_id uuid DEFAULT gen_random_uuid() NOT NULL,
    run_id uuid,
    job_type character varying(64) NOT NULL,
    status character varying(16) DEFAULT 'queued'::character varying NOT NULL,
    payload jsonb NOT NULL,
    idempotency_key character varying(255) NOT NULL,
    created_by character varying(255) NOT NULL,
    worker_id character varying(255),
    attempts integer DEFAULT 0 NOT NULL,
    max_attempts integer DEFAULT 3 NOT NULL,
    completed_items integer DEFAULT 0 NOT NULL,
    total_items integer DEFAULT 0 NOT NULL,
    cancellation_requested boolean DEFAULT false NOT NULL,
    lease_expires_at timestamp with time zone,
    last_error text,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    started_at timestamp with time zone,
    finished_at timestamp with time zone,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    next_attempt_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    failure_kind text,
    CONSTRAINT ck_vol_calibration_jobs_attempts CHECK (((attempts >= 0) AND (max_attempts > 0) AND (attempts <= max_attempts))),
    CONSTRAINT ck_vol_calibration_jobs_progress CHECK (((completed_items >= 0) AND (total_items >= 0) AND (completed_items <= total_items))),
    CONSTRAINT ck_vol_calibration_jobs_status CHECK (((status)::text = ANY ((ARRAY['queued'::character varying, 'running'::character varying, 'succeeded'::character varying, 'failed'::character varying, 'cancelled'::character varying])::text[]))),
    CONSTRAINT vol_calibration_jobs_failure_kind_check CHECK ((failure_kind = ANY (ARRAY['retryable'::text, 'permanent'::text])))
);


--
-- Name: vol_calibration_run_trade_inputs; Type: TABLE; Schema: at_lng; Owner: -
--

CREATE TABLE at_lng.vol_calibration_run_trade_inputs (
    run_id uuid NOT NULL,
    trade_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: vol_calibration_runs; Type: TABLE; Schema: at_lng; Owner: -
--

CREATE TABLE at_lng.vol_calibration_runs (
    run_id uuid DEFAULT gen_random_uuid() NOT NULL,
    commodity character varying(16) NOT NULL,
    cob_date date NOT NULL,
    input_fingerprint character varying(128) NOT NULL,
    engine_version character varying(64) NOT NULL,
    status character varying(16) DEFAULT 'draft'::character varying NOT NULL,
    configuration jsonb NOT NULL,
    notes text,
    created_by character varying(255) NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    submitted_at timestamp with time zone,
    idempotency_key character varying(255) NOT NULL,
    CONSTRAINT ck_vol_calibration_runs_status CHECK (((status)::text = ANY ((ARRAY['draft'::character varying, 'submitted'::character varying, 'approved'::character varying, 'published'::character varying, 'superseded'::character varying, 'rejected'::character varying])::text[])))
);


--
-- Name: vol_daily_artifacts; Type: TABLE; Schema: at_lng; Owner: -
--

CREATE TABLE at_lng.vol_daily_artifacts (
    job_id uuid NOT NULL,
    artifact_key text NOT NULL,
    codec text NOT NULL,
    digest text NOT NULL,
    raw_bytes integer NOT NULL,
    payload bytea NOT NULL,
    created_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    CONSTRAINT vol_daily_artifacts_codec_check CHECK ((codec = 'tagged-json-zlib-v1'::text)),
    CONSTRAINT vol_daily_artifacts_digest_check CHECK ((length(digest) = 64)),
    CONSTRAINT vol_daily_artifacts_raw_bytes_check CHECK (((raw_bytes >= 0) AND (raw_bytes <= 134217728)))
);


--
-- Name: vol_daily_stages; Type: TABLE; Schema: at_lng; Owner: -
--

CREATE TABLE at_lng.vol_daily_stages (
    job_id uuid NOT NULL,
    commodity text NOT NULL,
    cob_date date NOT NULL,
    stage text NOT NULL,
    publication_id uuid,
    details jsonb DEFAULT '{}'::jsonb NOT NULL,
    updated_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    CONSTRAINT vol_daily_stages_commodity_check CHECK ((commodity = ANY (ARRAY['BRENT'::text, 'HH'::text, 'TTF'::text, 'JKM'::text, 'NBP'::text]))),
    CONSTRAINT vol_daily_stages_stage_check CHECK ((stage = ANY (ARRAY['prepared'::text, 'calibrating'::text, 'accepted'::text, 'held'::text, 'staging'::text, 'staged'::text, 'publishing'::text, 'published'::text, 'verification_pending'::text, 'waiting'::text, 'cancelled'::text, 'verified'::text])))
);


--
-- Name: vol_intraday_active; Type: TABLE; Schema: at_lng; Owner: -
--

CREATE TABLE at_lng.vol_intraday_active (
    commodity text NOT NULL,
    business_date date NOT NULL,
    publication_id uuid NOT NULL,
    base_publication_id uuid NOT NULL,
    draft_id uuid NOT NULL,
    activated_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    CONSTRAINT ck_intraday_active_commodity CHECK ((commodity = ANY (ARRAY['TTF'::text, 'BRENT'::text])))
);


--
-- Name: vol_intraday_drafts; Type: TABLE; Schema: at_lng; Owner: -
--

CREATE TABLE at_lng.vol_intraday_drafts (
    draft_id uuid NOT NULL,
    actor text NOT NULL,
    candidate_id text NOT NULL,
    content_hash text NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    commodity text GENERATED ALWAYS AS (COALESCE((((payload -> 'candidate'::text) -> 'manifest'::text) ->> 'commodity'::text), 'TTF'::text)) STORED,
    CONSTRAINT ck_intraday_draft_commodity CHECK ((COALESCE((((payload -> 'candidate'::text) -> 'manifest'::text) ->> 'commodity'::text), 'TTF'::text) = ANY (ARRAY['TTF'::text, 'BRENT'::text])))
);


--
-- Name: vol_market_forwards; Type: TABLE; Schema: at_lng; Owner: -
--

CREATE TABLE at_lng.vol_market_forwards (
    forward_id uuid DEFAULT gen_random_uuid() NOT NULL,
    snapshot_id uuid NOT NULL,
    contract_date date NOT NULL,
    forward numeric(24,10) NOT NULL,
    source_quote_id character varying(255),
    vendor_published_at timestamp with time zone,
    ingested_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_vol_market_forwards_value CHECK ((forward > (0)::numeric))
);


--
-- Name: vol_market_option_quotes; Type: TABLE; Schema: at_lng; Owner: -
--

CREATE TABLE at_lng.vol_market_option_quotes (
    quote_id uuid DEFAULT gen_random_uuid() NOT NULL,
    snapshot_id uuid NOT NULL,
    contract_date date NOT NULL,
    option_expiration_date date NOT NULL,
    put_call character varying(1) NOT NULL,
    strike numeric(24,10) NOT NULL,
    mark_price numeric(24,10) NOT NULL,
    mark_iv numeric(18,10),
    volume numeric(24,6),
    open_interest numeric(24,6),
    source_quote_id character varying(255),
    vendor_published_at timestamp with time zone,
    ingested_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_vol_market_quotes_put_call CHECK (((put_call)::text = ANY ((ARRAY['C'::character varying, 'P'::character varying])::text[]))),
    CONSTRAINT ck_vol_market_quotes_values CHECK (((strike > (0)::numeric) AND (mark_price > (0)::numeric)))
);


--
-- Name: vol_market_snapshots; Type: TABLE; Schema: at_lng; Owner: -
--

CREATE TABLE at_lng.vol_market_snapshots (
    snapshot_id uuid DEFAULT gen_random_uuid() NOT NULL,
    commodity character varying(16) NOT NULL,
    business_date date NOT NULL,
    observed_at timestamp with time zone NOT NULL,
    source_name character varying(255) NOT NULL,
    source_revision character varying(255),
    input_fingerprint character varying(128) NOT NULL,
    status character varying(16) NOT NULL,
    option_quote_count integer NOT NULL,
    forward_count integer NOT NULL,
    metadata jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT ck_vol_market_snapshots_counts CHECK (((option_quote_count >= 0) AND (forward_count >= 0))),
    CONSTRAINT ck_vol_market_snapshots_status CHECK (((status)::text = ANY ((ARRAY['complete'::character varying, 'rejected'::character varying])::text[])))
);


--
-- Name: vol_publication_stage_points; Type: TABLE; Schema: at_lng; Owner: -
--

CREATE TABLE at_lng.vol_publication_stage_points (
    publication_id uuid NOT NULL,
    run_id uuid,
    commodity character varying(16),
    cob_date date,
    contract_date date NOT NULL,
    option_expiration_date date,
    strike numeric NOT NULL,
    delta numeric,
    put_call character varying(4),
    volatility numeric,
    total_variance numeric,
    working_forward numeric,
    surface_region character varying(16),
    blend_classification character varying(32),
    calibration_basis character varying(16),
    source_name character varying(255),
    calibration_method character varying(128),
    calibration_policy_version character varying(128),
    input_fingerprint character varying(128)
);


--
-- Name: vol_publication_stages; Type: TABLE; Schema: at_lng; Owner: -
--

CREATE TABLE at_lng.vol_publication_stages (
    publication_id uuid NOT NULL,
    run_id uuid NOT NULL,
    idempotency_key text NOT NULL,
    commodity text NOT NULL,
    created_by text NOT NULL,
    identity text NOT NULL,
    configuration jsonb NOT NULL,
    expiry_results jsonb NOT NULL,
    point_digest text NOT NULL,
    point_count integer NOT NULL,
    job_id uuid,
    ready boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    updated_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    CONSTRAINT vol_publication_stages_commodity_check CHECK ((commodity = ANY (ARRAY['BRENT'::text, 'HH'::text, 'TTF'::text, 'JKM'::text, 'NBP'::text]))),
    CONSTRAINT vol_publication_stages_identity_check CHECK ((length(identity) = 64)),
    CONSTRAINT vol_publication_stages_point_count_check CHECK ((point_count > 0)),
    CONSTRAINT vol_publication_stages_point_digest_check CHECK ((length(point_digest) = 64))
);


--
-- Name: vol_settlement_alerts; Type: TABLE; Schema: at_lng; Owner: -
--

CREATE TABLE at_lng.vol_settlement_alerts (
    alert_key text NOT NULL,
    commodity text NOT NULL,
    cob_date date NOT NULL,
    kind text NOT NULL,
    details jsonb DEFAULT '{}'::jsonb NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    first_seen_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    updated_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    resolved_at timestamp with time zone
);


--
-- Name: vol_settlement_calendars; Type: TABLE; Schema: at_lng; Owner: -
--

CREATE TABLE at_lng.vol_settlement_calendars (
    calendar_identity text NOT NULL,
    commodity text NOT NULL,
    from_cob date NOT NULL,
    through_cob date NOT NULL,
    manifest jsonb NOT NULL,
    imported_by text NOT NULL,
    imported_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    CONSTRAINT vol_settlement_calendars_check CHECK ((through_cob >= from_cob))
);


--
-- Name: vol_settlement_candidates; Type: TABLE; Schema: at_lng; Owner: -
--

CREATE TABLE at_lng.vol_settlement_candidates (
    job_id uuid NOT NULL,
    commodity text NOT NULL,
    cob_date date NOT NULL,
    input_identity text NOT NULL,
    state text NOT NULL,
    candidate jsonb,
    diagnostics jsonb DEFAULT '{}'::jsonb NOT NULL,
    publication_id uuid,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT vol_settlement_candidates_commodity_check CHECK ((commodity = ANY (ARRAY['BRENT'::text, 'HH'::text]))),
    CONSTRAINT vol_settlement_candidates_state_check CHECK ((state = ANY (ARRAY['prepared'::text, 'shadow'::text, 'published'::text, 'held'::text])))
);


--
-- Name: vol_settlement_promotion; Type: TABLE; Schema: at_lng; Owner: -
--

CREATE TABLE at_lng.vol_settlement_promotion (
    commodity text NOT NULL,
    shadow_start_cob date DEFAULT CURRENT_DATE NOT NULL,
    required_sessions integer DEFAULT 20 NOT NULL,
    approved boolean DEFAULT false NOT NULL,
    approved_by text,
    approved_at timestamp with time zone,
    evidence_through_cob date,
    evidence_identity text,
    CONSTRAINT ck_settlement_promotion_evidence CHECK (((NOT approved) OR ((evidence_through_cob IS NOT NULL) AND (evidence_identity IS NOT NULL)))),
    CONSTRAINT vol_settlement_promotion_check CHECK (((NOT approved) OR ((approved_by IS NOT NULL) AND (approved_at IS NOT NULL)))),
    CONSTRAINT vol_settlement_promotion_commodity_check CHECK ((commodity = ANY (ARRAY['BRENT'::text, 'HH'::text]))),
    CONSTRAINT vol_settlement_promotion_required_sessions_check CHECK ((required_sessions >= 20))
);


--
-- Name: vol_settlement_retry_audit; Type: TABLE; Schema: at_lng; Owner: -
--

CREATE TABLE at_lng.vol_settlement_retry_audit (
    retry_id uuid DEFAULT gen_random_uuid() NOT NULL,
    job_id uuid NOT NULL,
    actor text NOT NULL,
    reason text NOT NULL,
    prior_attempts integer NOT NULL,
    prior_max_attempts integer NOT NULL,
    new_max_attempts integer NOT NULL,
    created_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    CONSTRAINT vol_settlement_retry_audit_actor_check CHECK ((length(TRIM(BOTH FROM actor)) > 0)),
    CONSTRAINT vol_settlement_retry_audit_reason_check CHECK ((length(TRIM(BOTH FROM reason)) > 0))
);


--
-- Name: vol_settlement_sessions; Type: TABLE; Schema: at_lng; Owner: -
--

CREATE TABLE at_lng.vol_settlement_sessions (
    commodity text NOT NULL,
    cob_date date NOT NULL,
    ready_at timestamp with time zone NOT NULL,
    deadline_at timestamp with time zone NOT NULL,
    calendar_identity text NOT NULL,
    calendar_version text NOT NULL,
    calendar_source text NOT NULL,
    imported_by text NOT NULL,
    imported_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    CONSTRAINT vol_settlement_sessions_check CHECK ((deadline_at > ready_at)),
    CONSTRAINT vol_settlement_sessions_commodity_check CHECK ((commodity = ANY (ARRAY['BRENT'::text, 'HH'::text])))
);


--
-- Name: vol_settlement_source_events; Type: TABLE; Schema: at_lng; Owner: -
--

CREATE TABLE at_lng.vol_settlement_source_events (
    event_identity text NOT NULL,
    commodity text NOT NULL,
    cob_date date NOT NULL,
    snapshot_id uuid,
    state text NOT NULL,
    reason text,
    job_id uuid,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT vol_settlement_source_events_state_check CHECK ((state = ANY (ARRAY['waiting'::text, 'queued'::text, 'held'::text])))
);


--
-- Name: implied_volatility_surface_calibrated ck_calibrated_surface_brent_source_policy; Type: CHECK CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE at_lng.implied_volatility_surface_calibrated
    ADD CONSTRAINT ck_calibrated_surface_brent_source_policy CHECK ((((commodity)::text <> 'BRENT'::text) OR (((calibration_policy_version)::text ~~ 'brent_%'::text) AND ((calibration_method)::text ~~* '%SVI%'::text) AND ((source_name)::text ~~* '%Bloomberg Brent%'::text)))) NOT VALID;


--
-- Name: implied_volatility_surface_calibrated ck_calibrated_surface_euro_gas_source_policy; Type: CHECK CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE at_lng.implied_volatility_surface_calibrated
    ADD CONSTRAINT ck_calibrated_surface_euro_gas_source_policy CHECK ((((commodity)::text <> ALL (ARRAY[('JKM'::character varying)::text, ('NBP'::character varying)::text, ('TTF'::character varying)::text])) OR ((source_name)::text = 'official'::text) OR ((source_name)::text ~~* 'official:arbitrage_projection%'::text) OR ((source_name)::text ~* (('^official_surface_euro_gas_lng_v[0-9]+_smile_template_v[0-9]+'::text || chr(58)) || 'extrap$'::text)) OR (((commodity)::text = 'TTF'::text) AND ((source_name)::text ~ '^tfo-intraday-v1:[0-9a-f-]{36}$'::text)) OR COALESCE((((commodity)::text = 'TTF'::text) AND ((source_name)::text ~ '^tfo-intraday-smooth-v2:[0-9a-f-]{36}$'::text) AND ((calibration_method)::text = 'C2 constrained call-price spline'::text) AND ((calibration_policy_version)::text = 'smooth-call-spline-v1'::text) AND ((surface_region)::text = 'smooth_price'::text) AND ((blend_classification)::text = 'continuous_c2'::text) AND ((calibration_basis)::text = 'observed'::text) AND (total_variance > (0)::numeric) AND (volatility > (0)::numeric) AND (working_forward > (0)::numeric) AND (strike > (0)::numeric) AND (delta > (0)::numeric) AND (delta < (1)::numeric)), false))) NOT VALID;


--
-- Name: implied_volatility_surface_calibrated ck_calibrated_surface_five_product_dense_policy; Type: CHECK CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE at_lng.implied_volatility_surface_calibrated
    ADD CONSTRAINT ck_calibrated_surface_five_product_dense_policy CHECK ((((commodity)::text <> ALL (ARRAY[('BRENT'::character varying)::text, ('HH'::character varying)::text, ('JKM'::character varying)::text, ('NBP'::character varying)::text, ('TTF'::character varying)::text])) OR ((total_variance > (0)::numeric) AND (working_forward > (0)::numeric) AND (strike > (0)::numeric) AND (delta > (0)::numeric) AND (delta < (1)::numeric) AND (surface_region IS NOT NULL) AND (blend_classification IS NOT NULL) AND ((calibration_basis)::text = ANY (ARRAY[('observed'::character varying)::text, ('extrapolated'::character varying)::text])) AND (((calibration_method)::text ~~* '%PCHIP-core/Wing-v2-tail%'::text) OR (((commodity)::text = 'BRENT'::text) AND ((calibration_method)::text = 'single_svi_actual_strikes'::text) AND ((calibration_policy_version)::text = ANY (ARRAY['brent_single_svi_actual_strikes_v1'::text, 'brent_single_svi_actual_strikes_v2'::text])) AND ((surface_region)::text = 'single_svi'::text)) OR (((commodity)::text = 'HH'::text) AND ((calibration_method)::text = 'single_svi_seasonal_quotes'::text) AND ((calibration_policy_version)::text = ANY (ARRAY['hh_lne_single_svi_seasonal_quotes_v1'::text, 'hh_lne_single_svi_seasonal_quotes_v2'::text])) AND ((surface_region)::text = 'single_svi'::text)) OR (((commodity)::text = 'TTF'::text) AND ((calibration_method)::text = 'PCHIP/convex-call-price core with Wing-v2 tails'::text) AND ((calibration_policy_version)::text = 'ttf_quote_preserving_core_wing_tail_hybrid_v2'::text) AND ((surface_region)::text = ANY (ARRAY[('core'::character varying)::text, ('tail'::character varying)::text])) AND ((blend_classification)::text = ANY (ARRAY[('pchip_core'::character varying)::text, ('convex_call_core'::character varying)::text, ('left_blend'::character varying)::text, ('right_blend'::character varying)::text, ('wing_left'::character varying)::text, ('wing_right'::character varying)::text])))) AND (calibration_policy_version IS NOT NULL)) OR COALESCE((((commodity)::text = 'TTF'::text) AND ((source_name)::text ~ '^tfo-intraday-smooth-v2:[0-9a-f-]{36}$'::text) AND ((calibration_method)::text = 'C2 constrained call-price spline'::text) AND ((calibration_policy_version)::text = 'smooth-call-spline-v1'::text) AND ((surface_region)::text = 'smooth_price'::text) AND ((blend_classification)::text = 'continuous_c2'::text) AND ((calibration_basis)::text = 'observed'::text) AND (total_variance > (0)::numeric) AND (volatility > (0)::numeric) AND (working_forward > (0)::numeric) AND (strike > (0)::numeric) AND (delta > (0)::numeric) AND (delta < (1)::numeric)), false) OR COALESCE((((commodity)::text = 'HH'::text) AND ((calibration_method)::text = 'positive_lognormal_mixture'::text) AND ((calibration_policy_version)::text = 'hh_lne_sofr_positive_mixture_v1'::text) AND ((surface_region)::text = 'positive_mixture'::text) AND ((blend_classification)::text = ANY ((ARRAY['quote_mixture'::character varying, 'seasonal_mixture'::character varying])::text[])) AND ((calibration_basis)::text = ANY ((ARRAY['observed'::character varying, 'extrapolated'::character varying])::text[])) AND (total_variance > (0)::numeric) AND (volatility > (0)::numeric) AND (working_forward > (0)::numeric) AND (strike > (0)::numeric) AND (delta > (0)::numeric) AND (delta < (1)::numeric) AND ((source_name)::text ~~* '%LNE settlement%'::text) AND ((source_name)::text !~~* '%ICE PHE%'::text) AND ((source_name)::text !~~* '% ON %'::text)), false))) NOT VALID;


--
-- Name: implied_volatility_surface_calibrated ck_calibrated_surface_hh_source_policy; Type: CHECK CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE at_lng.implied_volatility_surface_calibrated
    ADD CONSTRAINT ck_calibrated_surface_hh_source_policy CHECK ((((commodity)::text <> 'HH'::text) OR (((calibration_policy_version)::text ~~ 'hh_lne_%'::text) AND (((calibration_method)::text ~~* '%LNE%'::text) OR (((calibration_method)::text = 'single_svi_seasonal_quotes'::text) AND ((calibration_policy_version)::text = ANY (ARRAY['hh_lne_single_svi_seasonal_quotes_v1'::text, 'hh_lne_single_svi_seasonal_quotes_v2'::text])))) AND ((source_name)::text ~~* '%LNE settlement%'::text) AND ((source_name)::text !~~* '%ICE PHE%'::text) AND ((source_name)::text !~~* '% ON %'::text)) OR COALESCE((((commodity)::text = 'HH'::text) AND ((calibration_method)::text = 'positive_lognormal_mixture'::text) AND ((calibration_policy_version)::text = 'hh_lne_sofr_positive_mixture_v1'::text) AND ((surface_region)::text = 'positive_mixture'::text) AND ((blend_classification)::text = ANY ((ARRAY['quote_mixture'::character varying, 'seasonal_mixture'::character varying])::text[])) AND ((calibration_basis)::text = ANY ((ARRAY['observed'::character varying, 'extrapolated'::character varying])::text[])) AND (total_variance > (0)::numeric) AND (volatility > (0)::numeric) AND (working_forward > (0)::numeric) AND (strike > (0)::numeric) AND (delta > (0)::numeric) AND (delta < (1)::numeric) AND ((source_name)::text ~~* '%LNE settlement%'::text) AND ((source_name)::text !~~* '%ICE PHE%'::text) AND ((source_name)::text !~~* '% ON %'::text)), false))) NOT VALID;


--
-- Name: implied_volatility_surface_calibrated implied_volatility_surface_calibrated_pkey; Type: CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.implied_volatility_surface_calibrated
    ADD CONSTRAINT implied_volatility_surface_calibrated_pkey PRIMARY KEY (surface_point_id);


--
-- Name: live_forward_curve_activations live_forward_curve_activations_pkey; Type: CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.live_forward_curve_activations
    ADD CONSTRAINT live_forward_curve_activations_pkey PRIMARY KEY (scope, version);


--
-- Name: live_forward_curve_heads live_forward_curve_heads_pkey; Type: CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.live_forward_curve_heads
    ADD CONSTRAINT live_forward_curve_heads_pkey PRIMARY KEY (scope);


--
-- Name: live_forward_curve_revisions live_forward_curve_revisions_pkey; Type: CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.live_forward_curve_revisions
    ADD CONSTRAINT live_forward_curve_revisions_pkey PRIMARY KEY (revision);


--
-- Name: option_expiry_calendar option_expiry_calendar_pkey; Type: CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.option_expiry_calendar
    ADD CONSTRAINT option_expiry_calendar_pkey PRIMARY KEY (calendar_id);


--
-- Name: option_expiry_calendar uq_option_expiry_calendar_contract; Type: CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.option_expiry_calendar
    ADD CONSTRAINT uq_option_expiry_calendar_contract UNIQUE (commodity, contract_date, exchange);


--
-- Name: vol_calibration_expiry_results uq_vol_calibration_expiry_run_expiry; Type: CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.vol_calibration_expiry_results
    ADD CONSTRAINT uq_vol_calibration_expiry_run_expiry UNIQUE (run_id, option_expiration_date);


--
-- Name: vol_calibration_job_items uq_vol_calibration_job_items_expiry; Type: CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.vol_calibration_job_items
    ADD CONSTRAINT uq_vol_calibration_job_items_expiry UNIQUE (job_id, option_expiration_date);


--
-- Name: vol_calibration_jobs uq_vol_calibration_jobs_idempotency; Type: CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.vol_calibration_jobs
    ADD CONSTRAINT uq_vol_calibration_jobs_idempotency UNIQUE (idempotency_key);


--
-- Name: vol_calibration_runs uq_vol_calibration_runs_idempotency; Type: CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.vol_calibration_runs
    ADD CONSTRAINT uq_vol_calibration_runs_idempotency UNIQUE (idempotency_key);


--
-- Name: vol_market_forwards uq_vol_market_forwards_snapshot_contract; Type: CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.vol_market_forwards
    ADD CONSTRAINT uq_vol_market_forwards_snapshot_contract UNIQUE (snapshot_id, contract_date);


--
-- Name: vol_market_option_quotes uq_vol_market_quotes_snapshot_contract; Type: CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.vol_market_option_quotes
    ADD CONSTRAINT uq_vol_market_quotes_snapshot_contract UNIQUE (snapshot_id, contract_date, put_call, strike);


--
-- Name: vol_market_snapshots uq_vol_market_snapshots_fingerprint; Type: CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.vol_market_snapshots
    ADD CONSTRAINT uq_vol_market_snapshots_fingerprint UNIQUE (commodity, input_fingerprint);


--
-- Name: vol_surface_publications uq_vol_surface_publications_idempotency; Type: CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.vol_surface_publications
    ADD CONSTRAINT uq_vol_surface_publications_idempotency UNIQUE (idempotency_key);


--
-- Name: vol_surface_publications uq_vol_surface_publications_run; Type: CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.vol_surface_publications
    ADD CONSTRAINT uq_vol_surface_publications_run UNIQUE (run_id);


--
-- Name: vol_calibration_audit_events vol_calibration_audit_events_pkey; Type: CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.vol_calibration_audit_events
    ADD CONSTRAINT vol_calibration_audit_events_pkey PRIMARY KEY (event_id);


--
-- Name: vol_calibration_expiry_results vol_calibration_expiry_results_pkey; Type: CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.vol_calibration_expiry_results
    ADD CONSTRAINT vol_calibration_expiry_results_pkey PRIMARY KEY (result_id);


--
-- Name: vol_calibration_intraday_trades vol_calibration_intraday_trades_pkey; Type: CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.vol_calibration_intraday_trades
    ADD CONSTRAINT vol_calibration_intraday_trades_pkey PRIMARY KEY (trade_id);


--
-- Name: vol_calibration_job_items vol_calibration_job_items_pkey; Type: CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.vol_calibration_job_items
    ADD CONSTRAINT vol_calibration_job_items_pkey PRIMARY KEY (item_id);


--
-- Name: vol_calibration_jobs vol_calibration_jobs_pkey; Type: CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.vol_calibration_jobs
    ADD CONSTRAINT vol_calibration_jobs_pkey PRIMARY KEY (job_id);


--
-- Name: vol_calibration_run_trade_inputs vol_calibration_run_trade_inputs_pkey; Type: CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.vol_calibration_run_trade_inputs
    ADD CONSTRAINT vol_calibration_run_trade_inputs_pkey PRIMARY KEY (run_id, trade_id);


--
-- Name: vol_calibration_runs vol_calibration_runs_pkey; Type: CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.vol_calibration_runs
    ADD CONSTRAINT vol_calibration_runs_pkey PRIMARY KEY (run_id);


--
-- Name: vol_daily_artifacts vol_daily_artifacts_pkey; Type: CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.vol_daily_artifacts
    ADD CONSTRAINT vol_daily_artifacts_pkey PRIMARY KEY (job_id, artifact_key);


--
-- Name: vol_daily_stages vol_daily_stages_pkey; Type: CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.vol_daily_stages
    ADD CONSTRAINT vol_daily_stages_pkey PRIMARY KEY (job_id);


--
-- Name: vol_intraday_active vol_intraday_active_pkey; Type: CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.vol_intraday_active
    ADD CONSTRAINT vol_intraday_active_pkey PRIMARY KEY (commodity, business_date);


--
-- Name: vol_intraday_drafts vol_intraday_drafts_actor_content_hash_key; Type: CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.vol_intraday_drafts
    ADD CONSTRAINT vol_intraday_drafts_actor_content_hash_key UNIQUE (actor, content_hash);


--
-- Name: vol_intraday_drafts vol_intraday_drafts_pkey; Type: CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.vol_intraday_drafts
    ADD CONSTRAINT vol_intraday_drafts_pkey PRIMARY KEY (draft_id);


--
-- Name: vol_market_forwards vol_market_forwards_pkey; Type: CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.vol_market_forwards
    ADD CONSTRAINT vol_market_forwards_pkey PRIMARY KEY (forward_id);


--
-- Name: vol_market_option_quotes vol_market_option_quotes_pkey; Type: CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.vol_market_option_quotes
    ADD CONSTRAINT vol_market_option_quotes_pkey PRIMARY KEY (quote_id);


--
-- Name: vol_market_snapshots vol_market_snapshots_pkey; Type: CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.vol_market_snapshots
    ADD CONSTRAINT vol_market_snapshots_pkey PRIMARY KEY (snapshot_id);


--
-- Name: vol_publication_stage_points vol_publication_stage_points_pkey; Type: CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.vol_publication_stage_points
    ADD CONSTRAINT vol_publication_stage_points_pkey PRIMARY KEY (publication_id, contract_date, strike);


--
-- Name: vol_publication_stages vol_publication_stages_idempotency_key_key; Type: CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.vol_publication_stages
    ADD CONSTRAINT vol_publication_stages_idempotency_key_key UNIQUE (idempotency_key);


--
-- Name: vol_publication_stages vol_publication_stages_pkey; Type: CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.vol_publication_stages
    ADD CONSTRAINT vol_publication_stages_pkey PRIMARY KEY (publication_id);


--
-- Name: vol_publication_stages vol_publication_stages_run_id_key; Type: CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.vol_publication_stages
    ADD CONSTRAINT vol_publication_stages_run_id_key UNIQUE (run_id);


--
-- Name: vol_settlement_alerts vol_settlement_alerts_pkey; Type: CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.vol_settlement_alerts
    ADD CONSTRAINT vol_settlement_alerts_pkey PRIMARY KEY (alert_key);


--
-- Name: vol_settlement_calendars vol_settlement_calendars_pkey; Type: CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.vol_settlement_calendars
    ADD CONSTRAINT vol_settlement_calendars_pkey PRIMARY KEY (calendar_identity);


--
-- Name: vol_settlement_candidates vol_settlement_candidates_pkey; Type: CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.vol_settlement_candidates
    ADD CONSTRAINT vol_settlement_candidates_pkey PRIMARY KEY (job_id);


--
-- Name: vol_settlement_promotion vol_settlement_promotion_pkey; Type: CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.vol_settlement_promotion
    ADD CONSTRAINT vol_settlement_promotion_pkey PRIMARY KEY (commodity);


--
-- Name: vol_settlement_retry_audit vol_settlement_retry_audit_pkey; Type: CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.vol_settlement_retry_audit
    ADD CONSTRAINT vol_settlement_retry_audit_pkey PRIMARY KEY (retry_id);


--
-- Name: vol_settlement_sessions vol_settlement_sessions_pkey; Type: CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.vol_settlement_sessions
    ADD CONSTRAINT vol_settlement_sessions_pkey PRIMARY KEY (commodity, cob_date);


--
-- Name: vol_settlement_source_events vol_settlement_source_events_pkey; Type: CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.vol_settlement_source_events
    ADD CONSTRAINT vol_settlement_source_events_pkey PRIMARY KEY (event_identity);


--
-- Name: vol_surface_publications vol_surface_publications_pkey; Type: CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.vol_surface_publications
    ADD CONSTRAINT vol_surface_publications_pkey PRIMARY KEY (publication_id);


--
-- Name: ix_calibrated_surface_publication_expiry; Type: INDEX; Schema: at_lng; Owner: -
--

CREATE INDEX ix_calibrated_surface_publication_expiry ON at_lng.implied_volatility_surface_calibrated USING btree (publication_id, option_expiration_date);


--
-- Name: ix_intraday_draft_actor_product; Type: INDEX; Schema: at_lng; Owner: -
--

CREATE INDEX ix_intraday_draft_actor_product ON at_lng.vol_intraday_drafts USING btree (actor, commodity, created_at DESC);


--
-- Name: ix_ttf_intraday_trades_business_expiry; Type: INDEX; Schema: at_lng; Owner: -
--

CREATE INDEX ix_ttf_intraday_trades_business_expiry ON at_lng.vol_calibration_intraday_trades USING btree (business_date, contract_date, observed_at);


--
-- Name: ix_vol_calibration_jobs_claim; Type: INDEX; Schema: at_lng; Owner: -
--

CREATE INDEX ix_vol_calibration_jobs_claim ON at_lng.vol_calibration_jobs USING btree (status, lease_expires_at, created_at);


--
-- Name: ix_vol_calibration_runs_product_cob; Type: INDEX; Schema: at_lng; Owner: -
--

CREATE INDEX ix_vol_calibration_runs_product_cob ON at_lng.vol_calibration_runs USING btree (commodity, cob_date, created_at);


--
-- Name: ix_vol_market_quotes_snapshot_expiry; Type: INDEX; Schema: at_lng; Owner: -
--

CREATE INDEX ix_vol_market_quotes_snapshot_expiry ON at_lng.vol_market_option_quotes USING btree (snapshot_id, contract_date);


--
-- Name: ix_vol_market_snapshots_latest; Type: INDEX; Schema: at_lng; Owner: -
--

CREATE INDEX ix_vol_market_snapshots_latest ON at_lng.vol_market_snapshots USING btree (commodity, business_date, observed_at);


--
-- Name: uq_calibrated_surface_publication_contract_strike; Type: INDEX; Schema: at_lng; Owner: -
--

CREATE UNIQUE INDEX uq_calibrated_surface_publication_contract_strike ON at_lng.implied_volatility_surface_calibrated USING btree (publication_id, contract_date, strike);


--
-- Name: uq_vol_surface_publications_active_product_cob; Type: INDEX; Schema: at_lng; Owner: -
--

CREATE UNIQUE INDEX uq_vol_surface_publications_active_product_cob ON at_lng.vol_surface_publications USING btree (commodity, cob_date) WHERE is_active;


--
-- Name: vol_daily_stages_cob_date_commodity_idx; Type: INDEX; Schema: at_lng; Owner: -
--

CREATE INDEX vol_daily_stages_cob_date_commodity_idx ON at_lng.vol_daily_stages USING btree (cob_date, commodity);


--
-- Name: vol_settlement_candidates_commodity_cob_date_state_idx; Type: INDEX; Schema: at_lng; Owner: -
--

CREATE INDEX vol_settlement_candidates_commodity_cob_date_state_idx ON at_lng.vol_settlement_candidates USING btree (commodity, cob_date, state);


--
-- Name: vol_surface_publications brent_intraday_settlement_guard; Type: TRIGGER; Schema: at_lng; Owner: -
--

CREATE TRIGGER brent_intraday_settlement_guard BEFORE INSERT OR UPDATE ON at_lng.vol_surface_publications FOR EACH ROW EXECUTE FUNCTION at_lng.guard_brent_intraday_settlement();


--
-- Name: vol_daily_artifacts daily_artifact_immutable; Type: TRIGGER; Schema: at_lng; Owner: -
--

CREATE TRIGGER daily_artifact_immutable BEFORE DELETE OR UPDATE ON at_lng.vol_daily_artifacts FOR EACH ROW EXECUTE FUNCTION at_lng.reject_daily_artifact_mutation();


--
-- Name: vol_intraday_active intraday_activation_guard; Type: TRIGGER; Schema: at_lng; Owner: -
--

CREATE TRIGGER intraday_activation_guard BEFORE INSERT OR UPDATE ON at_lng.vol_intraday_active FOR EACH ROW EXECUTE FUNCTION at_lng.guard_intraday_activation();


--
-- Name: vol_intraday_drafts intraday_draft_immutable; Type: TRIGGER; Schema: at_lng; Owner: -
--

CREATE TRIGGER intraday_draft_immutable BEFORE DELETE OR UPDATE ON at_lng.vol_intraday_drafts FOR EACH ROW EXECUTE FUNCTION at_lng.reject_intraday_draft_mutation();


--
-- Name: vol_publication_stages publication_stage_immutable; Type: TRIGGER; Schema: at_lng; Owner: -
--

CREATE TRIGGER publication_stage_immutable BEFORE UPDATE ON at_lng.vol_publication_stages FOR EACH ROW EXECUTE FUNCTION at_lng.protect_publication_stage();


--
-- Name: vol_publication_stage_points publication_stage_point_guard; Type: TRIGGER; Schema: at_lng; Owner: -
--

CREATE TRIGGER publication_stage_point_guard BEFORE INSERT OR DELETE OR UPDATE ON at_lng.vol_publication_stage_points FOR EACH ROW EXECUTE FUNCTION at_lng.protect_staged_point();


--
-- Name: implied_volatility_surface_calibrated trg_implied_volatility_surface_calibrated_append_only; Type: TRIGGER; Schema: at_lng; Owner: -
--

CREATE TRIGGER trg_implied_volatility_surface_calibrated_append_only BEFORE DELETE OR UPDATE ON at_lng.implied_volatility_surface_calibrated FOR EACH ROW EXECUTE FUNCTION at_lng.reject_vol_calibration_mutation();


--
-- Name: vol_calibration_intraday_trades trg_ttf_intraday_trades_guard; Type: TRIGGER; Schema: at_lng; Owner: -
--

CREATE TRIGGER trg_ttf_intraday_trades_guard BEFORE DELETE OR UPDATE ON at_lng.vol_calibration_intraday_trades FOR EACH ROW EXECUTE FUNCTION at_lng.guard_ttf_intraday_trade_mutation();


--
-- Name: vol_calibration_audit_events trg_vol_calibration_audit_events_append_only; Type: TRIGGER; Schema: at_lng; Owner: -
--

CREATE TRIGGER trg_vol_calibration_audit_events_append_only BEFORE DELETE OR UPDATE ON at_lng.vol_calibration_audit_events FOR EACH ROW EXECUTE FUNCTION at_lng.reject_vol_calibration_mutation();


--
-- Name: vol_calibration_expiry_results trg_vol_calibration_expiry_results_append_only; Type: TRIGGER; Schema: at_lng; Owner: -
--

CREATE TRIGGER trg_vol_calibration_expiry_results_append_only BEFORE DELETE OR UPDATE ON at_lng.vol_calibration_expiry_results FOR EACH ROW EXECUTE FUNCTION at_lng.reject_vol_calibration_mutation();


--
-- Name: vol_calibration_run_trade_inputs trg_vol_calibration_run_trade_inputs_append_only; Type: TRIGGER; Schema: at_lng; Owner: -
--

CREATE TRIGGER trg_vol_calibration_run_trade_inputs_append_only BEFORE DELETE OR UPDATE ON at_lng.vol_calibration_run_trade_inputs FOR EACH ROW EXECUTE FUNCTION at_lng.reject_vol_calibration_mutation();


--
-- Name: vol_calibration_runs trg_vol_calibration_runs_guard; Type: TRIGGER; Schema: at_lng; Owner: -
--

CREATE TRIGGER trg_vol_calibration_runs_guard BEFORE INSERT OR DELETE OR UPDATE ON at_lng.vol_calibration_runs FOR EACH ROW EXECUTE FUNCTION at_lng.guard_vol_calibration_run_mutation();


--
-- Name: vol_market_forwards trg_vol_market_forwards_append_only; Type: TRIGGER; Schema: at_lng; Owner: -
--

CREATE TRIGGER trg_vol_market_forwards_append_only BEFORE DELETE OR UPDATE ON at_lng.vol_market_forwards FOR EACH ROW EXECUTE FUNCTION at_lng.reject_vol_market_snapshot_mutation();


--
-- Name: vol_market_option_quotes trg_vol_market_option_quotes_append_only; Type: TRIGGER; Schema: at_lng; Owner: -
--

CREATE TRIGGER trg_vol_market_option_quotes_append_only BEFORE DELETE OR UPDATE ON at_lng.vol_market_option_quotes FOR EACH ROW EXECUTE FUNCTION at_lng.reject_vol_market_snapshot_mutation();


--
-- Name: vol_market_snapshots trg_vol_market_snapshots_append_only; Type: TRIGGER; Schema: at_lng; Owner: -
--

CREATE TRIGGER trg_vol_market_snapshots_append_only BEFORE DELETE OR UPDATE ON at_lng.vol_market_snapshots FOR EACH ROW EXECUTE FUNCTION at_lng.reject_vol_market_snapshot_mutation();


--
-- Name: vol_surface_publications trg_vol_surface_publications_guard; Type: TRIGGER; Schema: at_lng; Owner: -
--

CREATE TRIGGER trg_vol_surface_publications_guard BEFORE INSERT OR DELETE OR UPDATE ON at_lng.vol_surface_publications FOR EACH ROW EXECUTE FUNCTION at_lng.guard_vol_surface_publication_mutation();


--
-- Name: implied_volatility_surface_calibrated implied_volatility_surface_calibrated_publication_id_fkey; Type: FK CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.implied_volatility_surface_calibrated
    ADD CONSTRAINT implied_volatility_surface_calibrated_publication_id_fkey FOREIGN KEY (publication_id) REFERENCES at_lng.vol_surface_publications(publication_id) ON DELETE RESTRICT;


--
-- Name: implied_volatility_surface_calibrated implied_volatility_surface_calibrated_run_id_fkey; Type: FK CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.implied_volatility_surface_calibrated
    ADD CONSTRAINT implied_volatility_surface_calibrated_run_id_fkey FOREIGN KEY (run_id) REFERENCES at_lng.vol_calibration_runs(run_id) ON DELETE RESTRICT;


--
-- Name: live_forward_curve_activations live_forward_curve_activations_revision_fkey; Type: FK CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.live_forward_curve_activations
    ADD CONSTRAINT live_forward_curve_activations_revision_fkey FOREIGN KEY (revision) REFERENCES at_lng.live_forward_curve_revisions(revision);


--
-- Name: live_forward_curve_heads live_forward_curve_heads_revision_fkey; Type: FK CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.live_forward_curve_heads
    ADD CONSTRAINT live_forward_curve_heads_revision_fkey FOREIGN KEY (revision) REFERENCES at_lng.live_forward_curve_revisions(revision);


--
-- Name: vol_calibration_audit_events vol_calibration_audit_events_run_id_fkey; Type: FK CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.vol_calibration_audit_events
    ADD CONSTRAINT vol_calibration_audit_events_run_id_fkey FOREIGN KEY (run_id) REFERENCES at_lng.vol_calibration_runs(run_id) ON DELETE RESTRICT;


--
-- Name: vol_calibration_expiry_results vol_calibration_expiry_results_run_id_fkey; Type: FK CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.vol_calibration_expiry_results
    ADD CONSTRAINT vol_calibration_expiry_results_run_id_fkey FOREIGN KEY (run_id) REFERENCES at_lng.vol_calibration_runs(run_id) ON DELETE RESTRICT;


--
-- Name: vol_calibration_intraday_trades vol_calibration_intraday_trades_supersedes_trade_id_fkey; Type: FK CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.vol_calibration_intraday_trades
    ADD CONSTRAINT vol_calibration_intraday_trades_supersedes_trade_id_fkey FOREIGN KEY (supersedes_trade_id) REFERENCES at_lng.vol_calibration_intraday_trades(trade_id) ON DELETE RESTRICT;


--
-- Name: vol_calibration_job_items vol_calibration_job_items_job_id_fkey; Type: FK CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.vol_calibration_job_items
    ADD CONSTRAINT vol_calibration_job_items_job_id_fkey FOREIGN KEY (job_id) REFERENCES at_lng.vol_calibration_jobs(job_id) ON DELETE RESTRICT;


--
-- Name: vol_calibration_job_items vol_calibration_job_items_result_id_fkey; Type: FK CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.vol_calibration_job_items
    ADD CONSTRAINT vol_calibration_job_items_result_id_fkey FOREIGN KEY (result_id) REFERENCES at_lng.vol_calibration_expiry_results(result_id) ON DELETE RESTRICT;


--
-- Name: vol_calibration_jobs vol_calibration_jobs_run_id_fkey; Type: FK CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.vol_calibration_jobs
    ADD CONSTRAINT vol_calibration_jobs_run_id_fkey FOREIGN KEY (run_id) REFERENCES at_lng.vol_calibration_runs(run_id) ON DELETE RESTRICT;


--
-- Name: vol_calibration_run_trade_inputs vol_calibration_run_trade_inputs_run_id_fkey; Type: FK CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.vol_calibration_run_trade_inputs
    ADD CONSTRAINT vol_calibration_run_trade_inputs_run_id_fkey FOREIGN KEY (run_id) REFERENCES at_lng.vol_calibration_runs(run_id) ON DELETE RESTRICT;


--
-- Name: vol_calibration_run_trade_inputs vol_calibration_run_trade_inputs_trade_id_fkey; Type: FK CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.vol_calibration_run_trade_inputs
    ADD CONSTRAINT vol_calibration_run_trade_inputs_trade_id_fkey FOREIGN KEY (trade_id) REFERENCES at_lng.vol_calibration_intraday_trades(trade_id) ON DELETE RESTRICT;


--
-- Name: vol_daily_artifacts vol_daily_artifacts_job_id_fkey; Type: FK CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.vol_daily_artifacts
    ADD CONSTRAINT vol_daily_artifacts_job_id_fkey FOREIGN KEY (job_id) REFERENCES at_lng.vol_calibration_jobs(job_id);


--
-- Name: vol_daily_stages vol_daily_stages_job_id_fkey; Type: FK CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.vol_daily_stages
    ADD CONSTRAINT vol_daily_stages_job_id_fkey FOREIGN KEY (job_id) REFERENCES at_lng.vol_calibration_jobs(job_id);


--
-- Name: vol_daily_stages vol_daily_stages_publication_id_fkey; Type: FK CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.vol_daily_stages
    ADD CONSTRAINT vol_daily_stages_publication_id_fkey FOREIGN KEY (publication_id) REFERENCES at_lng.vol_surface_publications(publication_id);


--
-- Name: vol_intraday_active vol_intraday_active_base_publication_id_fkey; Type: FK CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.vol_intraday_active
    ADD CONSTRAINT vol_intraday_active_base_publication_id_fkey FOREIGN KEY (base_publication_id) REFERENCES at_lng.vol_surface_publications(publication_id);


--
-- Name: vol_intraday_active vol_intraday_active_draft_id_fkey; Type: FK CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.vol_intraday_active
    ADD CONSTRAINT vol_intraday_active_draft_id_fkey FOREIGN KEY (draft_id) REFERENCES at_lng.vol_intraday_drafts(draft_id);


--
-- Name: vol_intraday_active vol_intraday_active_publication_id_fkey; Type: FK CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.vol_intraday_active
    ADD CONSTRAINT vol_intraday_active_publication_id_fkey FOREIGN KEY (publication_id) REFERENCES at_lng.vol_surface_publications(publication_id);


--
-- Name: vol_market_forwards vol_market_forwards_snapshot_id_fkey; Type: FK CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.vol_market_forwards
    ADD CONSTRAINT vol_market_forwards_snapshot_id_fkey FOREIGN KEY (snapshot_id) REFERENCES at_lng.vol_market_snapshots(snapshot_id) ON DELETE RESTRICT;


--
-- Name: vol_market_option_quotes vol_market_option_quotes_snapshot_id_fkey; Type: FK CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.vol_market_option_quotes
    ADD CONSTRAINT vol_market_option_quotes_snapshot_id_fkey FOREIGN KEY (snapshot_id) REFERENCES at_lng.vol_market_snapshots(snapshot_id) ON DELETE RESTRICT;


--
-- Name: vol_publication_stage_points vol_publication_stage_points_publication_id_fkey; Type: FK CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.vol_publication_stage_points
    ADD CONSTRAINT vol_publication_stage_points_publication_id_fkey FOREIGN KEY (publication_id) REFERENCES at_lng.vol_publication_stages(publication_id) ON DELETE CASCADE;


--
-- Name: vol_publication_stages vol_publication_stages_job_id_fkey; Type: FK CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.vol_publication_stages
    ADD CONSTRAINT vol_publication_stages_job_id_fkey FOREIGN KEY (job_id) REFERENCES at_lng.vol_calibration_jobs(job_id);


--
-- Name: vol_settlement_candidates vol_settlement_candidates_job_id_fkey; Type: FK CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.vol_settlement_candidates
    ADD CONSTRAINT vol_settlement_candidates_job_id_fkey FOREIGN KEY (job_id) REFERENCES at_lng.vol_calibration_jobs(job_id);


--
-- Name: vol_settlement_candidates vol_settlement_candidates_publication_id_fkey; Type: FK CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.vol_settlement_candidates
    ADD CONSTRAINT vol_settlement_candidates_publication_id_fkey FOREIGN KEY (publication_id) REFERENCES at_lng.vol_surface_publications(publication_id);


--
-- Name: vol_settlement_retry_audit vol_settlement_retry_audit_job_id_fkey; Type: FK CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.vol_settlement_retry_audit
    ADD CONSTRAINT vol_settlement_retry_audit_job_id_fkey FOREIGN KEY (job_id) REFERENCES at_lng.vol_calibration_jobs(job_id);


--
-- Name: vol_settlement_source_events vol_settlement_source_events_job_id_fkey; Type: FK CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.vol_settlement_source_events
    ADD CONSTRAINT vol_settlement_source_events_job_id_fkey FOREIGN KEY (job_id) REFERENCES at_lng.vol_calibration_jobs(job_id);


--
-- Name: vol_surface_publications vol_surface_publications_run_id_fkey; Type: FK CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.vol_surface_publications
    ADD CONSTRAINT vol_surface_publications_run_id_fkey FOREIGN KEY (run_id) REFERENCES at_lng.vol_calibration_runs(run_id) ON DELETE RESTRICT;


--
-- Name: vol_surface_publications vol_surface_publications_supersedes_publication_id_fkey; Type: FK CONSTRAINT; Schema: at_lng; Owner: -
--

ALTER TABLE ONLY at_lng.vol_surface_publications
    ADD CONSTRAINT vol_surface_publications_supersedes_publication_id_fkey FOREIGN KEY (supersedes_publication_id) REFERENCES at_lng.vol_surface_publications(publication_id) ON DELETE RESTRICT;


--
-- PostgreSQL database dump complete
--


"""

BOOTSTRAP_CONFLICT_RELATIONS = (
    "brent_delta_history_points",
    "brent_delta_history_runs",
    "brent_delta_history_settlements",
    "brent_realized_history_runs",
    "brent_realized_history_settlements",
    "implied_volatility_surface_calibrated",
    "live_forward_curve_activations",
    "live_forward_curve_heads",
    "live_forward_curve_revisions",
    "option_expiry_calendar",
    "ttf_vol_surface_publication_current",
    "vol_calibration_audit_events",
    "vol_calibration_expiry_results",
    "vol_calibration_intraday_trades",
    "vol_calibration_job_items",
    "vol_calibration_jobs",
    "vol_calibration_run_trade_inputs",
    "vol_calibration_runs",
    "vol_daily_artifacts",
    "vol_daily_stages",
    "vol_intraday_active",
    "vol_intraday_drafts",
    "vol_market_forwards",
    "vol_market_option_quotes",
    "vol_market_snapshots",
    "vol_publication_stage_points",
    "vol_publication_stages",
    "vol_settlement_alerts",
    "vol_settlement_calendars",
    "vol_settlement_candidates",
    "vol_settlement_promotion",
    "vol_settlement_retry_audit",
    "vol_settlement_sessions",
    "vol_settlement_source_events",
    "vol_surface_publications",
)


def upgrade():
    db = op.get_bind()
    op.execute("SET LOCAL lock_timeout='5s'")
    op.execute("SET LOCAL statement_timeout='30s'")
    required = (
        "trades_options",
        "trades_options_valuation",
        "option_contract_conventions",
        "option_discount_curve_definitions",
    )
    missing = [
        n for n in required if not db.execute(text("SELECT to_regclass(:name)"), {"name": "at_lng." + n}).scalar()
    ]
    if missing:
        raise RuntimeError("Provision the external trade/catalogue schema first: " + ", ".join(missing))
    existing = (
        db.execute(
            text(
                "SELECT name FROM unnest(CAST(:names AS text[])) AS name WHERE to_regclass('at_lng.' || name) IS NOT NULL"
            ),
            {"names": list(BOOTSTRAP_CONFLICT_RELATIONS)},
        )
        .scalars()
        .all()
    )
    if existing:
        raise RuntimeError(
            "Existing unversioned dashboard schema: reconcile with the archived migration chain; do not stamp blindly"
        )
    _prepare_brent_components()
    _preserve_fractional_quantities()
    _prepare_option_families()
    db.exec_driver_sql(_FINAL_SCHEMA_SQL, execution_options={"no_parameters": True})
    db.execute(text("INSERT INTO at_lng.vol_settlement_promotion (commodity) VALUES ('BRENT'),('HH')"))
    db.execute(
        text(
            "INSERT INTO at_lng.option_discount_curve_definitions (curve_code,currency,day_count,compounding,source_name,active) VALUES ('USD_SOFR_BBG490_MID_V1','USD','ACT/365.25','continuous','BLOOMBERG',true) ON CONFLICT(curve_code) DO NOTHING"
        )
    )
    if tuple(
        db.execute(
            text(
                "SELECT currency,source_name,active FROM at_lng.option_discount_curve_definitions WHERE curve_code='USD_SOFR_BBG490_MID_V1'"
            )
        ).one()
    ) != ("USD", "BLOOMBERG", True):
        raise RuntimeError("Conflicting SOFR curve definition")


def downgrade():
    raise RuntimeError("Retain immutable audit/publication history; baseline downgrade is unsupported")
