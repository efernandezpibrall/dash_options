"""Bounded manual Bloomberg jobs and actor-scoped immutable price snapshots."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import subprocess
import socket
import sys
import tempfile
import threading
import time
from uuid import UUID, uuid4

from diskcache import Lock
from options.market_data.live_prices import (
    build_snapshot,
    fx_tenor_date,
    fx_value_date,
    as_date,
    validate_complete_market_snapshot,
    METHODOLOGY_VERSION,
    IMPLEMENTATION_FINGERPRINT,
)
from dash_options.core.config import config_value
from dash_options.core.snapshots import snapshot_store, SnapshotReferenceError
from dash_options.core.source_identity import source_config_fingerprint

LOG = logging.getLogger(__name__)
POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix='live-prices')
GUARD = threading.RLock()
FUTURES = {}
TTL = 24 * 60 * 60
JOB_TIMEOUT = 600
WORKER_START_TIMEOUT = 30


def namespace(actor):
    if not actor:
        raise ValueError('Authenticated price-view identity required')
    mappings = config_value('BLOOMBERG_OPTIONS', 'LIVE_PRICES_SPREAD_MAPPINGS')
    mapping_hash = hashlib.sha256(Path(mappings).expanduser().read_bytes()).hexdigest() if mappings else None
    identity = json.dumps([actor, source_config_fingerprint(),
                           config_value('BLOOMBERG_OPTIONS', 'LIVE_PRICES_QUOTE_TIMEZONE'), mapping_hash,
                           METHODOLOGY_VERSION, IMPLEMENTATION_FINGERPRINT],
                          separators=(',', ':'))
    return 'live-prices-v5:' + hashlib.sha256(identity.encode()).hexdigest()


def _retained_path(prefix):
    return Path.home() / '.local/share/dash-options/live-prices-retained' / (hashlib.sha256(prefix.encode()).hexdigest() + '.json')


def _retain_complete(prefix, reference, payload):
    """Keep one owner-scoped complete snapshot outside the expiring/evictable cache."""
    validate_complete_market_snapshot(payload['view'], require_fresh_fx=True)
    record = {'reference': reference, 'payload': payload}
    content = json.dumps(record, sort_keys=True, separators=(',', ':'), allow_nan=False)
    envelope = json.dumps({'record': record, 'sha256': hashlib.sha256(content.encode()).hexdigest()}, allow_nan=False)
    path = _retained_path(prefix)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    fd, temporary = tempfile.mkstemp(prefix='.snapshot-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as handle:
            handle.write(envelope)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _retained_complete(prefix):
    path = _retained_path(prefix)
    if not path.is_file():
        return None
    envelope = json.loads(path.read_text())
    record = envelope['record']
    content = json.dumps(record, sort_keys=True, separators=(',', ':'), allow_nan=False)
    if hashlib.sha256(content.encode()).hexdigest() != envelope['sha256']:
        raise ValueError('Retained snapshot failed integrity validation')
    if (record['reference'].get('namespace') != prefix or
            record['payload']['view'].get('methodology_version') != METHODOLOGY_VERSION):
        raise ValueError('Retained snapshot identity differs from this view')
    validate_complete_market_snapshot(record['payload']['view'], require_fresh_fx=True)
    return record


def _collector_command(output):
    configured = (config_value('BLOOMBERG_OPTIONS', 'LIVE_PRICES_REPOSITORY_PATH') or
                  config_value('BLOOMBERG_OPTIONS', 'REPOSITORY_PATH'))
    repository = Path(configured).expanduser() if configured else Path.home() / 'Repositories' / 'BBG'
    script = repository / 'live_prices.py'
    interpreter = config_value('BLOOMBERG_OPTIONS', 'LIVE_PRICES_PYTHON', fallback=sys.executable)
    if not script.is_file() or not Path(interpreter).is_file():
        raise ValueError('Bloomberg Live Prices collector is not configured; check BBG repository and interpreter settings.')
    quote_timezone = config_value('BLOOMBERG_OPTIONS', 'LIVE_PRICES_QUOTE_TIMEZONE')
    if not quote_timezone:
        raise ValueError('Set LIVE_PRICES_QUOTE_TIMEZONE to the verified Bloomberg quote timezone.')
    command = [interpreter, '-B', str(script), '--output', str(output), '--quality-curve',
               '--quote-timezone', quote_timezone]
    mappings = config_value('BLOOMBERG_OPTIONS', 'LIVE_PRICES_SPREAD_MAPPINGS')
    if mappings:
        command.extend(['--spread-mappings', str(Path(mappings).expanduser())])
    return command, repository


def _prepare_source(raw):
    # Prior numeric inputs come from the actual prior-day history. Their value
    # dates are reconstructed from the provider's historical spot settlement.
    for payload in (raw['fx'], raw['prior_fx']):
        spot = as_date(payload['spot_date'])
        if spot != fx_value_date(as_date(payload['as_of_date'])):
            raise ValueError('EUR/USD spot value date differs from the reviewed calendar')
        for pillar in payload['pillars']:
            expected = fx_tenor_date(spot, pillar['tenor']).isoformat()
            if pillar.get('value_date') and pillar['value_date'] != expected:
                raise ValueError(f"Bloomberg {pillar['tenor']} value date differs from the reviewed FX calendar")
            pillar['value_date'] = expected
    return raw


def _capture_source(progress=None, *, force_prepare=False):
    """Connect to one source-scoped local worker, starting it only on Refresh.

    The owner-only Unix socket is shared across web workers. Its identity pins
    configuration, collector code, timezone and spread mappings; it cannot reuse
    another release's prepared inputs. Published views remain actor-scoped.
    """
    command, repository = _collector_command(Path('unused'))
    script = repository / 'live_prices_worker.py'
    if not script.is_file():
        raise ValueError('The paired Bloomberg Live Prices worker is unavailable.')
    flags = ['--quote-timezone', command[command.index('--quote-timezone') + 1]]
    if '--spread-mappings' in command:
        flags.extend(['--spread-mappings', command[command.index('--spread-mappings') + 1]])
    files = [repository / name for name in ('live_prices.py', 'live_prices_worker.py',
                                           'option_chain_downloader.py', 'settlements_downloader.py')]
    identity = [command[0], str(repository.resolve()), source_config_fingerprint(), flags,
                [hashlib.sha256(path.read_bytes()).hexdigest() for path in files]]
    if '--spread-mappings' in flags:
        identity.append(hashlib.sha256(Path(flags[-1]).read_bytes()).hexdigest())
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24]
    directory = Path(tempfile.gettempdir()) / f'live-prices-{os.getuid()}'
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077:
        raise ValueError('Live Prices worker directory must be private and owned by this user.')
    endpoint = directory / (digest + '.sock')

    def connect():
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.settimeout(JOB_TIMEOUT)
            connection.connect(str(endpoint))
            return connection
        except Exception:
            connection.close()
            raise

    try:
        connection = connect()
    except (FileNotFoundError, ConnectionRefusedError):
        # Only a missing/refused socket permits restart; a slow live capture does not.
        fd = os.open(directory / (digest + '.lock'), os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, 'a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                connection = connect()
            except (FileNotFoundError, ConnectionRefusedError):
                endpoint.unlink(missing_ok=True)
                log_fd = os.open(directory / (digest + '.log'), os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
                with os.fdopen(log_fd, 'a') as log:
                    process = subprocess.Popen(
                        [command[0], '-B', str(script), '--socket', str(endpoint), *flags], cwd=repository,
                        env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'},
                        stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
                deadline = time.monotonic() + WORKER_START_TIMEOUT
                while True:
                    try:
                        connection = connect()
                        break
                    except (FileNotFoundError, ConnectionRefusedError):
                        if process.poll() is not None or time.monotonic() >= deadline:
                            raise RuntimeError('Bloomberg Live Prices worker did not become ready') from None
                        time.sleep(.05)
    with connection:
        deadline = time.monotonic() + JOB_TIMEOUT
        connection.sendall((json.dumps({'operation': 'capture', 'force_prepare': force_prepare}) + '\n').encode())
        with connection.makefile('rb') as stream:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('Bloomberg capture timed out')
                connection.settimeout(remaining)
                line = stream.readline(8 * 1024 * 1024 + 1)
                if not line or len(line) > 8 * 1024 * 1024:
                    raise RuntimeError('Bloomberg worker response unavailable or too large')
                message = json.loads(line)
                if message.get('stage') and progress:
                    progress(message['stage'])
                if message.get('status') == 'complete':
                    return _prepare_source(message['raw'])
                if message.get('status') == 'failed':
                    raise RuntimeError('Bloomberg refresh failed. Check the collector log and source access.')


def submit(actor, tab_id=None):
    tab_id = str(UUID(tab_id)) if tab_id else str(uuid4())
    prefix = namespace(actor)
    scope = prefix + ':tab:' + tab_id
    with GUARD, snapshot_store.cache.transact():
        active = snapshot_store.cache.get(scope)
        state = snapshot_store.cache.get(active) if active else None
        if state and state['status'] in {'queued', 'running'} and time.time() - state['created'] < JOB_TIMEOUT:
            return {'key': active, 'tab': tab_id}
        # Other tabs may join work that has not begun its quote acquisition yet.
        # Once acquisition starts, a later click requires a new capture.
        shared_key = snapshot_store.cache.get(prefix + ':pending')
        shared_state = snapshot_store.cache.get(shared_key) if shared_key else None
        if (shared_state and shared_state['status'] in {'queued', 'running'} and
                shared_state.get('accepting_join', False) and time.time() - shared_state['created'] < JOB_TIMEOUT):
            snapshot_store.cache.set(scope, shared_key, expire=TTL)
            return {'key': shared_key, 'tab': tab_id}
        if len(FUTURES) >= 4:
            raise ValueError('Bloomberg refresh queue is busy. Try again shortly.')
        key = prefix + ':job:' + str(uuid4())
        state = {'status': 'queued', 'stage': 'Waiting for Bloomberg', 'created': time.time(), 'accepting_join': True}
        snapshot_store.cache.set(key, state, expire=TTL)
        snapshot_store.cache.set(scope, key, expire=TTL)
        snapshot_store.cache.set(prefix + ':pending', key, expire=TTL)

        def run():
            with GUARD:
                pass  # Submission commits before the worker starts.
            try:
                with Lock(snapshot_store.cache, 'live-prices:collector-lock', expire=JOB_TIMEOUT):
                    if time.time() - state['created'] > JOB_TIMEOUT:
                        raise TimeoutError('Bloomberg refresh expired while queued')
                    started = time.monotonic()
                    state.update(status='running', stage='Preparing Bloomberg capture',
                                 timings={'queue_seconds': time.time() - state['created']})
                    snapshot_store.cache.set(key, state, expire=TTL)
                    def progress(stage):
                        state.update(stage=stage)
                        if stage.startswith('Capturing current') or stage.endswith('-month commodity quotes'):
                            state['accepting_join'] = False
                        snapshot_store.cache.set(key, state, expire=TTL)
                    raw = _capture_source(progress, force_prepare=snapshot_store.cache.pop(prefix + ':rebuild-prepared', False))
                    state['timings']['collector_seconds'] = time.monotonic() - started
                # Calculation and publication do not hold the Bloomberg connection lock.
                calculation_started = time.monotonic()
                state.update(stage='Calculating and validating all market curves', accepting_join=False)
                snapshot_store.cache.set(key, state, expire=TTL)
                view = build_snapshot(raw)
                validate_complete_market_snapshot(view, require_fresh_fx=True)
                state['timings']['calculation_seconds'] = time.monotonic() - calculation_started
                publication_started = time.monotonic()
                digest = hashlib.sha256(json.dumps(raw, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                ref = snapshot_store.publish(prefix, digest, {'raw': raw, 'view': view}, force=True, ttl_seconds=TTL)
                snapshot_store.resolve(ref, expected_namespace=prefix)
                _retain_complete(prefix, ref, {'raw': raw, 'view': view})
                state['timings'].update(publication_seconds=time.monotonic() - publication_started,
                                        total_seconds=time.time() - state['created'])
                state.update(status='complete', ref=ref, stage='Snapshot ready')
                LOG.info('Live Prices refresh timings: %s', state['timings'])
            except Exception as exc:
                LOG.exception('Live Prices refresh failed')
                # An incomplete pricing input must not remain cached across retry.
                snapshot_store.cache.set(prefix + ':rebuild-prepared', True, expire=TTL)
                state.update(status='failed', error=(str(exc) if isinstance(exc, ValueError)
                                                    else 'Bloomberg refresh unavailable. Your previous snapshot is retained.'))
                try:
                    retained = _retained_complete(prefix)
                    if retained:
                        state['retained_ref'] = retained['reference']
                        state['error'] += ' Showing the last complete snapshot at its original capture time.'
                except Exception:
                    LOG.exception('Retained Live Prices snapshot could not be recovered')
            finally:
                snapshot_store.cache.set(key, state, expire=TTL)
                with GUARD:
                    FUTURES.pop(key, None)
        FUTURES[key] = POOL.submit(run)
    return {'key': key, 'tab': tab_id}


def poll(ticket, actor):
    prefix = namespace(actor) + ':job:'
    if not isinstance(ticket, dict) or not str(ticket.get('key', '')).startswith(prefix):
        raise ValueError('Price refresh does not belong to this user')
    state = snapshot_store.cache.get(ticket['key'])
    if not state:
        raise ValueError('Price refresh has expired. Click Refresh again.')
    if state['status'] in {'queued', 'running'} and time.time() - state['created'] > JOB_TIMEOUT + 10:
        return {**state, 'status': 'failed', 'error': 'Bloomberg refresh timed out. Click Refresh to retry.'}
    return state


def resolve(reference, actor):
    if isinstance(reference, dict) and reference.get('kind') == 'desk_curve':
        from dash_options.shared.live_forwards import resolve as resolve_curve
        curve = resolve_curve(reference['revision'])
        return {**curve.view(), 'desk_curve_revision': curve.revision}
    prefix = namespace(actor)
    try:
        return snapshot_store.resolve(reference, expected_namespace=prefix)['view']
    except SnapshotReferenceError:
        # Recover only the exact issued reference, never a newer snapshot in an old tab.
        retained = _retained_complete(prefix)
        if retained and retained['reference'] == reference:
            return retained['payload']['view']
        raise
