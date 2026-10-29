from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import text

import dash_options.core.config as runtime_config
from options.connection_pool import database_pool_snapshot
from options.vol_calibration.io.database import postgres_engine


@pytest.fixture(autouse=True)
def configuration(monkeypatch, tmp_path):
    runtime_config.clear_runtime_config_cache()
    for key in ('DATABASE_URL', 'OPTIONS_DATABASE_URL', 'MIDDLE_OFFICE_DATABASE_URL'):
        monkeypatch.delenv(key, raising=False)
    path = tmp_path / 'config.ini'
    path.write_text(f'[DATABASE]\nCONNECTION_STRING=sqlite:///{tmp_path}/main.db\n')
    monkeypatch.setenv('OPTIONS_CONFIG_PATH', str(path))
    yield path
    runtime_config.clear_runtime_config_cache()


def test_all_call_forms_and_analytics_share_one_pool(configuration):
    barrier = Barrier(12)
    forms = [{}, {'required': False}, {'required': True}, {'middle_office': True}]
    def acquire(i):
        barrier.wait(timeout=5)
        engine = runtime_config.get_database_engine(**forms[i % len(forms)])
        with engine.connect() as connection:
            assert connection.scalar(text('SELECT 1')) == 1
        return engine
    with ThreadPoolExecutor(max_workers=12) as workers:
        engines = list(workers.map(acquire, range(12)))
    assert all(engine is engines[0] for engine in engines)
    assert postgres_engine(str(configuration)) is engines[0]
    assert len(database_pool_snapshot()['pools']) == 1


def test_separate_middle_office_database_has_a_separate_bounded_pool(configuration, tmp_path):
    with configuration.open('a') as file:
        file.write(f'MIDDLE_OFFICE_CONNECTION_STRING=sqlite:///{tmp_path}/middle-office.db\n')
    main = runtime_config.get_database_engine()
    middle = runtime_config.get_database_engine(middle_office=True)
    assert main is not middle
    assert main.url.database != middle.url.database
    assert database_pool_snapshot()['reserved_capacity'] == 10


def test_missing_optional_database_does_not_create_a_pool(configuration):
    configuration.write_text('')
    assert runtime_config.get_database_engine(required=False) is None
    with pytest.raises(RuntimeError, match='unavailable'):
        runtime_config.get_database_engine(required=True)
    assert database_pool_snapshot()['pools'] == []
