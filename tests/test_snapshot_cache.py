import json

import pytest

from snapshot_cache import SnapshotReferenceError, SnapshotStore


def test_snapshot_reference_is_small_versioned_and_cross_process_safe(tmp_path):
    first_worker = SnapshotStore(tmp_path, ttl_seconds=60)
    second_worker = SnapshotStore(tmp_path, ttl_seconds=60)
    payload = {'rows': [{'value': index} for index in range(1000)]}

    reference = first_worker.publish(
        'test-v1',
        'source-revision-7',
        payload,
        metadata={'rows': 1000},
    )

    assert reference['schema_version'] == 1
    assert reference['namespace'] == 'test-v1'
    assert reference['source_revision'] == 'source-revision-7'
    assert len(json.dumps(reference)) < 300
    assert second_worker.resolve(reference, expected_namespace='test-v1') == payload

    first_worker.close()
    second_worker.close()


def test_snapshot_reference_rejects_wrong_consumer_and_force_revisions(tmp_path):
    store = SnapshotStore(tmp_path, ttl_seconds=60)
    first = store.publish('test-v1', 'revision', {'value': 1})
    reused = store.publish('test-v1', 'revision', {'value': 1})
    forced = store.publish('test-v1', 'revision', {'value': 2}, force=True)

    assert reused['snapshot_id'] == first['snapshot_id']
    assert forced['snapshot_id'] != first['snapshot_id']
    assert store.resolve(first, expected_namespace='test-v1') == {'value': 1}
    assert store.resolve(forced, expected_namespace='test-v1') == {'value': 2}
    with pytest.raises(SnapshotReferenceError):
        store.resolve(first, expected_namespace='another-page-v1')
    store.close()
