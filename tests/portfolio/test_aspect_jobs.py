import pytest
from dash_options.core.snapshots import SnapshotStore
from dash_options.features.portfolio import aspect_jobs as jobs


@pytest.fixture
def isolated(monkeypatch,tmp_path):
    store=SnapshotStore(tmp_path)
    monkeypatch.setattr(jobs,'snapshot_store',store)
    monkeypatch.setattr(jobs,'config_bool',lambda *a,**k:True)
    monkeypatch.setattr(jobs.service,'configured_scope',lambda:'portfolio-scope')
    monkeypatch.setattr(jobs,'source_config_fingerprint',lambda:'configuration')
    yield store
    store.close()


def test_duplicate_refresh_is_one_worker_and_failed_job_can_retry(isolated):
    launched=[]
    first=jobs.submit('settlement','2026-10-29','user',launch=launched.append)
    duplicate=jobs.submit('settlement','2026-10-29','another-user',launch=launched.append)
    assert first == duplicate and len(launched)==1
    jobs.progress(first['job_id'],'valuing')
    assert jobs.poll(first)['phase']=='valuing'
    jobs.finish(first['job_id'],error='source unavailable')
    assert jobs.poll(first)['error']=='source unavailable'
    retry=jobs.submit('settlement','2026-10-29','user',launch=launched.append)
    assert retry != first and len(launched)==2
    jobs.finish(retry['job_id'],snapshot_id='immutable-snapshot')
    assert jobs.poll(retry)['snapshot_id']=='immutable-snapshot'


def test_jobs_are_bounded_and_dead_workers_release_request(isolated,monkeypatch):
    first=jobs.submit('settlement','2026-10-29','user',launch=lambda _:None)
    jobs.submit('settlement','2026-10-28','user',launch=lambda _:None)
    with pytest.raises(ValueError,match='busy'):
        jobs.submit('settlement','2026-10-27','user',launch=lambda _:None)
    state=jobs.poll(first)
    monkeypatch.setattr(jobs.time,'time',lambda:state['deadline']+1)
    assert jobs.poll(first)['status']=='failed'
    jobs.submit('settlement','2026-10-27','user',launch=lambda _:None)


def test_disabled_writes_do_not_launch_workers(isolated,monkeypatch):
    monkeypatch.setattr(jobs,'config_bool',lambda *a,**k:False)
    with pytest.raises(ValueError,match='disabled'):
        jobs.submit('settlement','2026-10-29','user',launch=lambda _:pytest.fail('Launched while disabled'))


def test_revalue_pins_received_source_and_deduplicates_without_fetch(isolated,monkeypatch):
    monkeypatch.setattr(jobs,'get_database_engine',lambda:object())
    monkeypatch.setattr(jobs.storage,'source_header',lambda *a,**k:{'snapshot_id':'saved-source'})
    launch=[]
    first=jobs.submit('imported','2026-10-29','user',action='revalue',launch=launch.append)
    second=jobs.submit('imported','2026-10-29','user',action='revalue',launch=launch.append)
    assert first==second and len(launch)==1
    state=jobs.poll(first)
    assert state['source_snapshot_id']=='saved-source' and state['action']=='revalue'
    monkeypatch.setattr(jobs.storage,'source_header',lambda *a,**k:None)
    with pytest.raises(ValueError,match='No received'):
        jobs.submit('settlement','2026-10-29','user',action='revalue',launch=launch.append)
