"""Execute one authorized Aspect refresh outside the HTTP worker."""
import logging
import signal
import sys
import time
from sqlalchemy import event
from options.aspect import service, storage
from options.aspect.errors import AspectWorkflowError
from dash_options.core.config import get_database_engine
from dash_options.core.database_reads import DB_SCHEMA
from dash_options.features.portfolio import aspect_jobs as jobs


def run(job_id):
    state = jobs.poll({'job_id':job_id})
    if state['status'] not in {'queued', 'running'}:
        return
    def timeout(*_):
        raise TimeoutError('Aspect refresh exceeded its execution deadline')
    previous = signal.signal(signal.SIGALRM, timeout)
    signal.alarm(max(1, int(state['deadline'] - time.time())))
    try:
        if service.configured_scope() != state['scope']:
            raise ValueError('Portfolio scope changed; submit a new refresh')
        engine = get_database_engine()
        @event.listens_for(engine, 'begin')
        def bounded_queries(connection):
            connection.exec_driver_sql("SET LOCAL statement_timeout='60s'")
            connection.exec_driver_sql("SET LOCAL lock_timeout='10s'")
        common = dict(engine=engine,schema=DB_SCHEMA,request_started_at=state['request_started_at'],
                      progress=lambda phase:jobs.progress(job_id,phase),cancel_check=lambda:time.time()>=state['deadline'])
        if state.get('action')=='revalue':
            source = storage.source_header(engine,state['source_snapshot_id'],schema=DB_SCHEMA)
            if not source or source['scope_key']!=state['scope'] or str(source['cob_date'])!=state['cob_date'] or source['pricing_mode']!=state['mode']:
                raise ValueError('Saved positions do not match this authorized job scope')
            result = service.revalue_snapshot(state['source_snapshot_id'],**common)
        else:
            result = service.refresh_snapshot(state['cob_date'],mode=state['mode'],**common)
        jobs.finish(job_id, snapshot_id=result['meta']['snapshot_id'])
    except Exception as exc:
        logging.exception('Aspect refresh failed')
        message = str(exc) if isinstance(exc, (AspectWorkflowError, TimeoutError, ValueError)) else 'Aspect refresh failed; see the worker log and reload the persisted snapshot before retrying.'
        jobs.finish(job_id,error=message)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


if __name__ == '__main__':
    run(sys.argv[1])
