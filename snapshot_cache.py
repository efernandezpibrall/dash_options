"""Process-safe, immutable server snapshots with small browser references."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import tempfile
from pathlib import Path
from typing import Any

from diskcache import Cache, Lock


SNAPSHOT_SCHEMA_VERSION = 1
DEFAULT_TTL_SECONDS = 60 * 60
DEFAULT_SIZE_LIMIT_BYTES = 512 * 1024 * 1024


class SnapshotReferenceError(RuntimeError):
    """Raised when a browser snapshot reference is invalid or unavailable."""


def _default_cache_directory() -> Path:
    configured = os.getenv('OPTIONS_SNAPSHOT_CACHE_DIR')
    if configured:
        return Path(configured).expanduser()
    return Path(tempfile.gettempdir()) / f'dash-options-snapshots-{os.getuid()}'


def _json_safe(value):
    return json.loads(json.dumps(value or {}, default=str))


class SnapshotStore:
    """Disk-backed snapshots shared by Gunicorn workers on the same host."""

    def __init__(self, directory=None, *, size_limit=None, ttl_seconds=None):
        self.directory = Path(directory or _default_cache_directory()).expanduser()
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            self.directory.chmod(0o700)
        except OSError:
            pass
        resolved_limit = size_limit or int(
            os.getenv('OPTIONS_SNAPSHOT_CACHE_SIZE_BYTES', DEFAULT_SIZE_LIMIT_BYTES)
        )
        self.ttl_seconds = int(
            ttl_seconds or os.getenv('OPTIONS_SNAPSHOT_TTL_SECONDS', DEFAULT_TTL_SECONDS)
        )
        self.cache = Cache(str(self.directory), size_limit=resolved_limit)

    @staticmethod
    def _identity(namespace: str, source_revision: str) -> str:
        encoded = json.dumps(
            {
                'namespace': namespace,
                'schema_version': SNAPSHOT_SCHEMA_VERSION,
                'source_revision': source_revision,
            },
            sort_keys=True,
            separators=(',', ':'),
        ).encode('utf-8')
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _snapshot_key(namespace: str, snapshot_id: str) -> str:
        return f'snapshot:{namespace}:{snapshot_id}'

    @staticmethod
    def _latest_key(namespace: str) -> str:
        return f'latest:{namespace}'

    def publish(
        self,
        namespace: str,
        source_revision: str,
        payload: Any,
        *,
        metadata=None,
        group: str | None = None,
        force: bool = False,
        ttl_seconds: int | None = None,
    ) -> dict:
        namespace = str(namespace or '').strip()
        source_revision = str(source_revision or '').strip()
        if not namespace or not source_revision:
            raise ValueError('namespace and source_revision are required')

        ttl = int(ttl_seconds or self.ttl_seconds)
        identity = self._identity(namespace, source_revision)
        with Lock(self.cache, f'lock:{namespace}:{identity}', expire=120):
            current = self.latest(namespace)
            if (
                not force
                and current
                and current.get('source_revision') == source_revision
            ):
                try:
                    self.resolve(current, expected_namespace=namespace)
                    return current
                except SnapshotReferenceError:
                    pass

            snapshot_id = secrets.token_urlsafe(24)
            reference = {
                'schema_version': SNAPSHOT_SCHEMA_VERSION,
                'namespace': namespace,
                'snapshot_id': snapshot_id,
                'source_revision': source_revision,
                'meta': _json_safe(metadata),
            }
            record = {
                'schema_version': SNAPSHOT_SCHEMA_VERSION,
                'namespace': namespace,
                'snapshot_id': snapshot_id,
                'source_revision': source_revision,
                'payload': payload,
            }
            tag = group or namespace
            snapshot_key = self._snapshot_key(namespace, snapshot_id)
            if not self.cache.set(snapshot_key, record, expire=ttl, tag=tag, retry=True):
                raise SnapshotReferenceError('Server snapshot could not be persisted')
            if not self.cache.set(
                self._latest_key(namespace),
                reference,
                expire=ttl,
                tag=tag,
                retry=True,
            ):
                self.cache.delete(snapshot_key, retry=True)
                raise SnapshotReferenceError('Server snapshot pointer could not be persisted')
            return reference

    def latest(self, namespace: str) -> dict | None:
        reference = self.cache.get(self._latest_key(namespace), retry=True)
        return reference if isinstance(reference, dict) else None

    def resolve(self, reference, *, expected_namespace: str | None = None):
        if not isinstance(reference, dict):
            raise SnapshotReferenceError('Snapshot reference is missing')
        if reference.get('schema_version') != SNAPSHOT_SCHEMA_VERSION:
            raise SnapshotReferenceError('Snapshot schema version is unsupported')
        namespace = reference.get('namespace')
        snapshot_id = reference.get('snapshot_id')
        source_revision = reference.get('source_revision')
        if expected_namespace and namespace != expected_namespace:
            raise SnapshotReferenceError('Snapshot namespace does not match the consumer')
        if not namespace or not snapshot_id or not source_revision:
            raise SnapshotReferenceError('Snapshot reference is incomplete')

        record = self.cache.get(
            self._snapshot_key(namespace, snapshot_id),
            default=None,
            retry=True,
        )
        if not isinstance(record, dict):
            raise SnapshotReferenceError('Server snapshot expired; refresh the page')
        expected = (
            SNAPSHOT_SCHEMA_VERSION,
            namespace,
            snapshot_id,
            source_revision,
        )
        actual = (
            record.get('schema_version'),
            record.get('namespace'),
            record.get('snapshot_id'),
            record.get('source_revision'),
        )
        if actual != expected:
            raise SnapshotReferenceError('Server snapshot failed identity validation')
        return record['payload']

    def clear_group(self, group: str) -> int:
        return int(self.cache.evict(group, retry=True) or 0)

    def generation(self, group: str) -> int:
        return int(self.cache.get(f'generation:{group}', default=0, retry=True) or 0)

    def bump_generation(self, group: str) -> int:
        return int(
            self.cache.incr(f'generation:{group}', delta=1, default=1, retry=True)
        )

    def lock(self, name: str, *, expire=300):
        return Lock(self.cache, f'lock:{name}', expire=expire)

    def close(self):
        self.cache.close()


snapshot_store = SnapshotStore()


def publish_snapshot(*args, **kwargs):
    return snapshot_store.publish(*args, **kwargs)


def latest_snapshot(namespace):
    return snapshot_store.latest(namespace)


def resolve_snapshot(reference, *, expected_namespace=None):
    return snapshot_store.resolve(reference, expected_namespace=expected_namespace)


def clear_snapshot_group(group):
    return snapshot_store.clear_group(group)


def snapshot_generation(group):
    return snapshot_store.generation(group)


def bump_snapshot_generation(group):
    return snapshot_store.bump_generation(group)


def snapshot_lock(name, *, expire=300):
    return snapshot_store.lock(name, expire=expire)
