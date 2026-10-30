"""Separate immutable received positions from repeatable valuation runs."""
from alembic import op
import sqlalchemy as sa

revision = '20261030_03'
down_revision = '20261030_02'
branch_labels = depends_on = None
S = 'at_lng'
RAW = ['instrument', 'pricing_date', 'quantity', 'source_unit', 'maturity_date', 'maturity_forward_date', 'strategy', 'book', 'deal_type', 'entity_type', 'exchange_trade_otc', 'currency', 'strike_price', 'put_call', 'buy_sell', 'counterparty', 'source_price']
VALUED = ['model', 'type_option', 'contract_convention_code', 'margin_style', 'pricing_model_application', 'pricing_model_version', 'convention_source_url', 'asset_a', 'asset_a_multiplier', 'adjusted_strike', 'forward_price_used', 'forward_source_name', 'forward_source_cob_date', 'volatility_used', 'atm_volatility', 'volatility_source_name', 'volatility_source_cob_date', 'volatility_method', 'volatility_adjustment_factor', 'volatility_adjustment_components', 'surface_contract_convention', 'surface_expiry_date', 'surface_expiry_status', 'volatility_reference_convention', 'volatility_reference_convention_version', 'variance_calendar_code', 'variance_calendar_version', 'business_days_to_trade_expiry', 'business_days_to_surface_expiry', 'trade_expiry_validation_status', 'trade_expiry_validation_reason', 'correlation', 'time_to_expiry', 'model_horizon_date', 'discount_curve_code', 'discount_curve_cob_date', 'discount_factor_to_expiry', 'discount_factor_to_model_horizon', 'asian_averaging_schedule', 'averaging_fixing_times', 'time_to_averaging_start', 'time_to_floating_price_determination', 'unit_value', 'unit_delta_asset_a', 'unit_gamma_asset_a', 'unit_vega_sigma1', 'unit_theta', 'unit_rho', 'unit_correlation_sensitivity', 'unit_intrinsic_value', 'unit_time_value', 'unit_vega_equivalent', 'booked_unit_premium', 'position_value', 'position_premium', 'position_pnl', 'position_delta_asset_a', 'position_delta_asset_b', 'position_gamma_asset_a', 'position_gamma_asset_b', 'position_cross_gamma', 'position_vega_sigma1', 'position_vega_sigma2', 'position_theta', 'position_rho', 'position_correlation_sensitivity', 'position_intrinsic_value', 'position_time_value']


def upgrade():
    c = op.get_bind()
    old_columns = [r['name'] for r in sa.inspect(c).get_columns('aspect_options_valuation', schema=S)]
    old_header = [r['name'] for r in sa.inspect(c).get_columns('aspect_options_valuation_snapshot', schema=S)]
    def sql(statement):
        c.execute(sa.text(statement))
    # Source headers retain all original receipt/audit metadata; valuation fields move to runs.
    sql(f'CREATE TABLE {S}.aspect_position_snapshot (LIKE {S}.aspect_options_valuation_snapshot INCLUDING DEFAULTS)')
    for name in ('is_current','valuation_started_at','valuation_finished_at'):
        sql(f'ALTER TABLE {S}.aspect_position_snapshot DROP COLUMN {name}')
    sql(f"""ALTER TABLE {S}.aspect_position_snapshot
        ADD PRIMARY KEY (snapshot_id),
        ADD UNIQUE (snapshot_id,cob_date,pricing_mode,scope_key),
        ADD COLUMN origin text NOT NULL DEFAULT 'webservice',
        ADD COLUMN ingestion_status text NOT NULL DEFAULT 'fetching',
        ADD COLUMN validation_status text NOT NULL DEFAULT 'pending',
        ADD COLUMN error_message text,
        ADD COLUMN request_parameters jsonb NOT NULL DEFAULT '{{}}',
        ADD COLUMN endpoint text,
        ADD COLUMN http_status integer,
        ADD COLUMN request_attempts integer,
        ADD COLUMN response_body text,
        ADD COLUMN confirmed_empty boolean NOT NULL DEFAULT false,
        ADD CHECK (pricing_mode IN ('settlement','live','imported')),
        ADD CHECK (ingestion_status IN ('fetching','received','failed')),
        ADD CHECK (validation_status IN ('pending','valid','partial','invalid')),
        ADD CHECK (source_rows >= 0 AND accepted_rows >= 0 AND rejected_rows >= 0),
        ADD CHECK (validation_status NOT IN ('valid','partial') OR source_rows=accepted_rows+rejected_rows)
    """)
    cols = [x for x in old_header if x not in {'is_current','valuation_started_at','valuation_finished_at'}]
    sql(f"INSERT INTO {S}.aspect_position_snapshot ({','.join(cols)},origin,ingestion_status,validation_status,confirmed_empty) SELECT {','.join(cols)},CASE WHEN raw_response IS NULL THEN 'legacy_import' ELSE 'legacy_webservice' END,'received',CASE WHEN rejected_rows>0 THEN 'partial' ELSE 'valid' END,accepted_rows=0 AND source_rows=0 FROM {S}.aspect_options_valuation_snapshot")
    sql(f'CREATE INDEX aspect_source_lookup_idx ON {S}.aspect_position_snapshot (cob_date,pricing_mode,scope_key,request_started_at DESC)')
    sql(f'CREATE TABLE {S}.aspect_position (LIKE {S}.aspect_options_valuation INCLUDING DEFAULTS)')
    for name in ['cob_date',*VALUED]:
        sql(f'ALTER TABLE {S}.aspect_position DROP COLUMN {name}')
    for name in RAW + ['is_option']:
        sql(f'ALTER TABLE {S}.aspect_position ALTER COLUMN {name} DROP NOT NULL')
    sql(f"""ALTER TABLE {S}.aspect_position ADD PRIMARY KEY(snapshot_id,source_row_id),
        ADD FOREIGN KEY(snapshot_id) REFERENCES {S}.aspect_position_snapshot(snapshot_id),
        ADD COLUMN original_row_index integer,
        ADD COLUMN validation_status text NOT NULL DEFAULT 'pending',
        ADD COLUMN validation_errors jsonb NOT NULL DEFAULT '[]',
        ADD CHECK(source_row_id>=0),
        ADD CHECK(validation_status IN ('pending','accepted','rejected')),
        ADD CHECK(validation_status<>'accepted' OR (instrument IS NOT NULL AND quantity IS NOT NULL AND maturity_forward_date IS NOT NULL AND is_option IS NOT NULL))
    """)
    cols = ['snapshot_id','source_row_id','is_option','raw_payload',*RAW]
    sql(f"INSERT INTO {S}.aspect_position ({','.join(cols)},validation_status) SELECT {','.join(cols)},'accepted' FROM {S}.aspect_options_valuation")
    sql(f"""CREATE TABLE {S}.aspect_valuation_run (
        run_id uuid PRIMARY KEY, source_snapshot_id uuid NOT NULL,
        cob_date date NOT NULL, pricing_mode text NOT NULL, scope_key text NOT NULL,
        request_started_at timestamptz NOT NULL DEFAULT now(),
        valuation_started_at timestamptz, valuation_finished_at timestamptz,
        persisted_at timestamptz NOT NULL DEFAULT now(),
        status text NOT NULL CHECK(status IN ('running','succeeded','failed')),
        is_current boolean NOT NULL DEFAULT false,
        error_message text, engine_revision text,
        input_manifest jsonb NOT NULL DEFAULT '{{}}',
        expected_option_rows integer NOT NULL CHECK(expected_option_rows>=0),
        valued_option_rows integer NOT NULL DEFAULT 0 CHECK(valued_option_rows>=0),
        UNIQUE(run_id,source_snapshot_id),
        FOREIGN KEY(source_snapshot_id,cob_date,pricing_mode,scope_key)
            REFERENCES {S}.aspect_position_snapshot(snapshot_id,cob_date,pricing_mode,scope_key),
        CHECK(NOT is_current OR status='succeeded'),
        CHECK(status<>'succeeded' OR expected_option_rows=valued_option_rows)
    )""")
    sql(f"""INSERT INTO {S}.aspect_valuation_run
        (run_id,source_snapshot_id,cob_date,pricing_mode,scope_key,request_started_at,valuation_started_at,valuation_finished_at,persisted_at,status,is_current,expected_option_rows,valued_option_rows)
        SELECT s.snapshot_id,s.snapshot_id,s.cob_date,s.pricing_mode,s.scope_key,s.request_started_at,s.valuation_started_at,s.valuation_finished_at,s.persisted_at,'succeeded',s.is_current,
               (SELECT count(*) FROM {S}.aspect_options_valuation p WHERE p.snapshot_id=s.snapshot_id AND p.is_option),
               (SELECT count(*) FROM {S}.aspect_options_valuation p WHERE p.snapshot_id=s.snapshot_id AND p.is_option)
        FROM {S}.aspect_options_valuation_snapshot s""")
    sql(f'CREATE UNIQUE INDEX aspect_valuation_current_uq ON {S}.aspect_valuation_run(cob_date,pricing_mode,scope_key) WHERE is_current')
    sql(f'CREATE INDEX aspect_valuation_source_idx ON {S}.aspect_valuation_run(source_snapshot_id,request_started_at DESC)')
    sql(f'CREATE TABLE {S}.aspect_option_valuation (LIKE {S}.aspect_options_valuation INCLUDING DEFAULTS)')
    for name in ['cob_date','is_option','raw_payload',*RAW]:
        sql(f'ALTER TABLE {S}.aspect_option_valuation DROP COLUMN {name}')
    sql(f'ALTER TABLE {S}.aspect_option_valuation RENAME COLUMN snapshot_id TO source_snapshot_id')
    sql(f"""ALTER TABLE {S}.aspect_option_valuation ADD COLUMN run_id uuid NOT NULL,
        ADD PRIMARY KEY(run_id,source_row_id),
        ADD FOREIGN KEY(run_id,source_snapshot_id) REFERENCES {S}.aspect_valuation_run(run_id,source_snapshot_id),
        ADD FOREIGN KEY(source_snapshot_id,source_row_id) REFERENCES {S}.aspect_position(snapshot_id,source_row_id),
        ADD CHECK(model IS NOT NULL AND asset_a IS NOT NULL AND position_value IS NOT NULL AND position_pnl IS NOT NULL AND position_delta_asset_a IS NOT NULL AND position_gamma_asset_a IS NOT NULL AND position_vega_sigma1 IS NOT NULL AND position_theta IS NOT NULL)
    """)
    assert not c.execute(sa.text(f"SELECT 1 FROM {S}.aspect_options_valuation WHERE NOT is_option AND ("+' OR '.join(x+' IS NOT NULL' for x in VALUED)+') LIMIT 1')).first(), 'Unexpected linear valuations require explicit migration'
    sql(f"INSERT INTO {S}.aspect_option_valuation (run_id,source_snapshot_id,source_row_id,{','.join(VALUED)}) SELECT snapshot_id,snapshot_id,source_row_id,{','.join(VALUED)} FROM {S}.aspect_options_valuation WHERE is_option")
    # Check the split by joining back before replacing legacy tables with read-only views.
    detail_select = ','.join(('r.run_id' if x=='snapshot_id' else 'r.cob_date' if x=='cob_date' else 'v.'+x if x in VALUED else 'p.'+x)+' AS '+x for x in old_columns)
    detail_query = f"SELECT {detail_select} FROM {S}.aspect_valuation_run r JOIN {S}.aspect_position p ON p.snapshot_id=r.source_snapshot_id AND p.validation_status='accepted' LEFT JOIN {S}.aspect_option_valuation v ON v.run_id=r.run_id AND v.source_row_id=p.source_row_id WHERE r.status='succeeded'"
    header_select = ','.join(('r.run_id' if x=='snapshot_id' else 'r.'+x if x in {'is_current','valuation_started_at','valuation_finished_at','persisted_at'} else 's.'+x)+' AS '+x for x in old_header)
    header_query = f"SELECT {header_select} FROM {S}.aspect_valuation_run r JOIN {S}.aspect_position_snapshot s ON s.snapshot_id=r.source_snapshot_id WHERE r.status='succeeded'"
    for table,query in [('aspect_options_valuation',detail_query),('aspect_options_valuation_snapshot',header_query)]:
        assert c.execute(sa.text(f'SELECT count(*) FROM ((SELECT * FROM {S}.{table} EXCEPT ALL ({query})) UNION ALL (({query}) EXCEPT ALL SELECT * FROM {S}.{table})) differences')).scalar_one()==0, 'Split changed '+table
    sql(f'DROP TABLE {S}.aspect_options_valuation')
    sql(f'DROP TABLE {S}.aspect_options_valuation_snapshot')
    sql(f'CREATE VIEW {S}.aspect_options_valuation AS {detail_query}')
    sql(f'CREATE VIEW {S}.aspect_options_valuation_snapshot AS {header_query}')


def downgrade():
    raise RuntimeError('Source snapshots and valuation history must be preserved; roll back using the read compatibility views.')
