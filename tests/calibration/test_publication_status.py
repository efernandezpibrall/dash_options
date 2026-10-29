"""Warnings, scoped progress and automatic refresh retain immutable snapshot selection."""

from copy import deepcopy

from dash import no_update
from sqlalchemy import event

from options.vol_calibration import api
from scripts.verification.verify_settlement_pipeline import fixture_inputs
from tests.helpers.publication import publish
from dash_options.calibration import publication_status


def test_publication_status_uses_small_projection_and_owner_scoped_jobs(database):
    candidate = api.build_settlement_candidate(fixture_inputs())
    receipt = publish(database, candidate, "status-publication", return_surface=False)
    queries = []
    def record(_conn, _cursor, query, _parameters, _context, _many):
        queries.append(query)
    event.listen(database, "before_cursor_execute", record)
    value = publication_status.load_publication_status(database, "2026-10-02", owner="someone-else")
    event.remove(database, "before_cursor_execute", record)
    assert value["publications"][0]["publication_id"] == receipt["publication_id"]
    assert value["jobs"] == []
    assert not any("implied_volatility_surface_calibrated" in q or "SELECT r.configuration " in q for q in queries)
    assert publication_status.load_publication_status(database, "2026-10-01")["publications"] == []


def test_warning_visibility_and_refresh_on_revision_change(monkeypatch):
    from dash_options.features.vol_surface import page as vol_surface
    import dash_options.core.config as runtime_config
    monkeypatch.setattr(runtime_config, "get_database_engine", lambda: object())
    value = {"revision": "first", "jobs": [], "publications": [{"commodity": "BRENT", "warnings": [
        {"code": "mean_quote_error_exceeded", "message": "BRENT mean 4.67 exceeds 2.20; published with warning."}
    ]}]}
    monkeypatch.setattr(publication_status, "load_publication_status", lambda *a, **kw: deepcopy(value))
    state, messages, clicks = vol_surface.poll_surface_publications(1, "2026-10-20", None, 0)
    assert "4.67" in str(messages) and clicks is no_update
    original = deepcopy(state)
    _, _, clicks = vol_surface.poll_surface_publications(2, "2026-10-20", state, 0)
    assert clicks is no_update
    value["revision"] = "second"
    new, _, clicks = vol_surface.poll_surface_publications(3, "2026-10-20", state, 0)
    assert clicks == 1 and new["revision"] == "second"
    assert state == original
    _, _, clicks = vol_surface.poll_surface_publications(4, "2026-10-16", state, 0)
    assert clicks is no_update


def test_status_notices_have_actionable_failure_and_success_tones():
    value = {'jobs':[
        {'commodity':'BRENT','stage':'verified','status':'succeeded','publication_id':'12345678-abc'},
        {'commodity':'HH','stage':'held','status':'failed','failure_kind':'permanent',
         'details':{'message':'Body maximum failed','failed_expiries':[{'expiry':'2026-11-01','message':'above maximum'}]}},
        {'commodity':'TTF','stage':'staging','status':'queued','failure_kind':'retryable','last_error':'QueryCanceled: retry available'},
    ], 'publications':[]}
    notices = publication_status.status_notices(value)
    assert [row['tone'] for row in notices] == ['success','error','warning']
    assert '12345678' in notices[0]['message']
    assert 'Body maximum failed' in notices[1]['message'] and '2026-11-01' in notices[1]['message']
    assert 'waiting to retry' in notices[2]['message']
    assert publication_status.status_messages(value) == [row['message'] for row in notices]


def test_uncertain_committed_job_is_presented_as_verification_pending():
    status = {'jobs':[{'commodity':'BRENT','stage':'publishing','status':'queued','failure_kind':'retryable',
                       'details':{'planned_publication_id':'12345678-abc'}}],
              'publications':[{'commodity':'BRENT','publication_id':'12345678-abc','warnings':[]}]}
    row, = publication_status.status_notices(status)
    assert row['tone'] == 'warning' and row['publication_id'] == '12345678-abc'
    assert 'verification pending' in row['message']
