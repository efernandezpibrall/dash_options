"""Version Aspect snapshots without discarding the existing import."""
import hashlib
import json
import uuid
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '20261030_02'
down_revision = '20261030_01'
branch_labels = depends_on = None
SCHEMA = 'at_lng'
HEADER = 'aspect_options_valuation_snapshot'
DETAIL = 'aspect_options_valuation'


def upgrade():
    op.add_column(HEADER, sa.Column('snapshot_id', postgresql.UUID()), schema=SCHEMA)
    op.add_column(DETAIL, sa.Column('snapshot_id', postgresql.UUID()), schema=SCHEMA)
    for column in (
        sa.Column('scope_key', sa.Text()),
        sa.Column('is_current', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('request_started_at', sa.DateTime(timezone=True)),
        sa.Column('valuation_started_at', sa.DateTime(timezone=True)),
        sa.Column('valuation_finished_at', sa.DateTime(timezone=True)),
        sa.Column('raw_response', postgresql.JSONB()),
        sa.Column('rejected_details', postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column('source_content_sha256', sa.Text()),
    ):
        op.add_column(HEADER, column, schema=SCHEMA)
    connection = op.get_bind()
    for row in connection.execute(sa.text(f'SELECT cob_date,strategy_filter,book_filter FROM {SCHEMA}.{HEADER}')).mappings():
        identity = json.dumps({'strategies': sorted(set(s.strip() for s in row['strategy_filter'].split(',') if s.strip())),
                               'book': row['book_filter'].strip()}, sort_keys=True, separators=(',', ':'))
        params = dict(cob=row['cob_date'], id=str(uuid.uuid4()), scope=hashlib.sha256(identity.encode()).hexdigest())
        connection.execute(sa.text(f'UPDATE {SCHEMA}.{HEADER} SET snapshot_id=CAST(:id AS uuid),scope_key=:scope,request_started_at=COALESCE(fetched_at,persisted_at) WHERE cob_date=:cob'), params)
        connection.execute(sa.text(f'UPDATE {SCHEMA}.{DETAIL} SET snapshot_id=CAST(:id AS uuid) WHERE cob_date=:cob'), params)
    op.drop_constraint('aspect_options_valuation_cob_date_fkey', DETAIL, schema=SCHEMA)
    op.drop_constraint('aspect_options_valuation_pkey', DETAIL, schema=SCHEMA)
    op.drop_constraint('aspect_options_valuation_snapshot_pkey', HEADER, schema=SCHEMA)
    for table in (HEADER, DETAIL):
        op.alter_column(table, 'snapshot_id', nullable=False, schema=SCHEMA)
    for column in ('scope_key', 'request_started_at'):
        op.alter_column(HEADER, column, nullable=False, schema=SCHEMA)
    op.create_primary_key('aspect_options_valuation_snapshot_pkey', HEADER, ['snapshot_id'], schema=SCHEMA)
    op.create_unique_constraint('aspect_snapshot_id_cob_uq', HEADER, ['snapshot_id','cob_date'], schema=SCHEMA)
    op.create_primary_key('aspect_options_valuation_pkey', DETAIL, ['snapshot_id','source_row_id'], schema=SCHEMA)
    op.create_foreign_key('aspect_options_valuation_snapshot_fkey', DETAIL, HEADER,
                         ['snapshot_id','cob_date'], ['snapshot_id','cob_date'], source_schema=SCHEMA, referent_schema=SCHEMA)
    op.create_index('aspect_snapshot_current_uq', HEADER, ['cob_date','pricing_mode','scope_key'],
                    unique=True, schema=SCHEMA, postgresql_where=sa.text('is_current'))
    op.drop_constraint('aspect_snapshot_count_ck', HEADER, schema=SCHEMA)
    op.create_check_constraint('aspect_snapshot_count_ck', HEADER,
                              'accepted_rows >= 0 AND rejected_rows >= 0 AND source_rows = accepted_rows + rejected_rows', schema=SCHEMA)


def downgrade():
    raise RuntimeError('Aspect version history is retained. Roll back application code with a version-aware reader; do not discard snapshots.')
