"""Bounded workstation refresh jobs with shared status and duplicate protection."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from dash_options.core.snapshots import snapshot_store
from dash_options.core.source_identity import source_config_fingerprint
from dash_options.core.config import config_bool
from options.aspect import service, storage
from dash_options.core.config import get_database_engine
from dash_options.core.database_reads import DB_SCHEMA
import pandas as pd

TTL = 24 * 3600
DEADLINE = 900
ACTIVE_KEY = 'aspect-refresh-active-v1'


def _namespace():
    return 'aspect-job:' + source_config_fingerprint() + ':' + os.getenv('APP_CODE_REVISION', 'checkout') + ':'


def _key(job_id):
    return _namespace() + str(uuid.UUID(job_id))


def _expired(state):
    return state['status'] in {'queued', 'running'} and time.time() > state['deadline']


def submit(mode, selected_date, actor, *, launch=None, action="refresh"):
    if not config_bool('ASPECT', 'PERSISTENCE_WRITES_ENABLED', fallback=False):
        raise ValueError('Aspect refresh writes are disabled.')
    if action not in {'refresh','revalue'}:
        raise ValueError('Unknown Aspect job action')
    cob = (pd.Timestamp(selected_date).date() if action=='revalue' else service.resolve_request_date(mode, selected_date)).isoformat()
    scope = service.configured_scope()
    source_id = None
    if action=='revalue':
        header = storage.source_header(get_database_engine(),cob_date=cob,mode=mode,scope=scope,schema=DB_SCHEMA,received_only=True)
        if header is None:
            raise ValueError('No received Aspect positions exist for this date and mode.')
        source_id = str(header['snapshot_id'])
    request_key = hashlib.sha256(json.dumps([_namespace(), cob, mode, scope, action, source_id]).encode()).hexdigest()
    cache = snapshot_store.cache
    with cache.transact():
        active = cache.get(ACTIVE_KEY, {})
        active = {k:v for k,v in active.items() if v['deadline'] > time.time()}
        if request_key in active:
            return {'job_id':active[request_key]['job_id']}
        if len(active) >= 2:
            raise ValueError('Aspect refresh queue is busy. Wait for the running refresh to finish.')
        job_id = str(uuid.uuid4())
        now = time.time()
        state = {'job_id':job_id, 'request_key':request_key, 'scope':scope, 'mode':mode, 'cob_date':cob,
                 'actor':actor, 'action':action, 'source_snapshot_id':source_id, 'status':'queued', 'phase':'queued', 'deadline':now + DEADLINE,
                 'request_started_at':datetime.now(timezone.utc).isoformat(), 'configuration':source_config_fingerprint()}
        cache.set(_key(job_id), state, expire=TTL)
        active[request_key] = {'job_id':job_id, 'deadline':state['deadline']}
        cache.set(ACTIVE_KEY, active, expire=TTL)
    try:
        (launch or _launch)(job_id)
    except Exception:
        finish(job_id, error='Refresh worker could not start. Retry the request.')
        raise ValueError('Refresh worker could not start. Retry the request.') from None
    return {'job_id':job_id}


def _launch(job_id):
    log_dir = Path(snapshot_store.directory) / 'aspect-jobs'
    log_dir.mkdir(mode=0o700, exist_ok=True)
    with (log_dir / f'{job_id}.log').open('ab') as log:
        subprocess.Popen([sys.executable, '-m', 'dash_options.features.portfolio.aspect_worker', job_id],
                         stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                         start_new_session=True, close_fds=True, env=os.environ.copy())


def poll(ticket):
    state = snapshot_store.cache.get(_key(ticket['job_id']))
    if not state:
        raise ValueError('Aspect refresh status expired. Reload the page.')
    if _expired(state):
        finish(state['job_id'], error='Refresh worker timed out. Reload the persisted snapshot before retrying.')
        state = snapshot_store.cache.get(_key(ticket['job_id']))
    return {k:v for k,v in state.items() if k not in {'actor','configuration','request_key'}}


def progress(job_id, phase):
    cache = snapshot_store.cache
    with cache.transact():
        state = cache.get(_key(job_id))
        if not state or _expired(state) or state['status'] == 'failed':
            raise TimeoutError('Refresh deadline exceeded')
        state.update(status='running', phase=phase)
        cache.set(_key(job_id), state, expire=TTL)


def finish(job_id, *, snapshot_id=None, error=None):
    cache = snapshot_store.cache
    with cache.transact():
        state = cache.get(_key(job_id))
        if state is None:
            return
        state.update(status='failed' if error else 'complete', phase='failed' if error else 'complete',
                     error=error, snapshot_id=snapshot_id)
        cache.set(_key(job_id), state, expire=TTL)
        active = cache.get(ACTIVE_KEY, {})
        if active.get(state['request_key'], {}).get('job_id') == job_id:
            active.pop(state['request_key'], None)
            cache.set(ACTIVE_KEY, active, expire=TTL)
