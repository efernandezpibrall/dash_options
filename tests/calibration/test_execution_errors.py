"""Driver failures and operator feedback preserve retry and redaction semantics."""

import psycopg2
import pytest
from sqlalchemy import exc as sa_exc

from dash_options.calibration.execution.errors import failure_details, retryable_execution, safe_failure_reason


@pytest.mark.parametrize('state,retry', [
    ('08006',True), ('40001',True), ('40P01',True), ('53300',True), ('55P03',True),
    ('57014',True), ('57P01',True), ('58030',True), ('42501',False), ('42P01',False),
    ('23505',False), ('22003',False), ('28P01',False),
])
def test_sqlstate_takes_precedence_for_raw_and_wrapped_driver_errors(state, retry):
    class DriverError(psycopg2.OperationalError):
        pgcode = state
    error = DriverError('password=private-marker SELECT * FROM sensitive')
    for value in (error, sa_exc.OperationalError('private SQL', {'secret':'private-marker'}, error)):
        assert retryable_execution(value) == retry
        message = safe_failure_reason(value)
        assert state in message
        assert 'private-marker' not in message and 'sensitive' not in message and 'private SQL' not in message


@pytest.mark.parametrize('error,retry', [
    (psycopg2.OperationalError('network'),True), (psycopg2.InterfaceError('closed'),True),
    (sa_exc.TimeoutError('pool'),True), (sa_exc.OperationalError('lost',{},Exception('lost')),True),
    (sa_exc.ProgrammingError('SQL',{},Exception('schema')),False), (ValueError('invalid'),False),
])
def test_unclassified_driver_and_pool_failures(error, retry):
    assert retryable_execution(error) == retry


def test_domain_feedback_is_bounded_redacted_and_retains_failed_expiry():
    error = ValueError('Body maximum failed; postgres://u:private-marker@host/db token=private-marker')
    error.diagnostics = {'results':[
        {'expiry':'2026-11-01','error':'shape check rejected'},
        {'expiry':'2026-12-01','error':'density rejected password="private-marker"'},
    ]}
    result = failure_details(error)
    assert result['failed_expiries'][0] == {'expiry':'2026-11-01','message':'shape check rejected'}
    assert 'Body maximum failed' in result['message'] and 'private-marker' not in str(result)
    assert len(safe_failure_reason(ValueError('x'*900))) == 500
