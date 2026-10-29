"""Real persistence contracts for compact publication reads and audit references."""

import hashlib
import json
from uuid import uuid4

import numpy as np
import pytest
from sqlalchemy import event, text

from options.vol_calibration import api
from scripts.verification.verify_settlement_pipeline import fixture_inputs
from tests.helpers.publication import publish


def test_internal_readback_keeps_canonical_precision_without_json_roundtrip(database, monkeypatch):
    from options.vol_calibration.publication import service
    candidate = api.build_settlement_candidate(fixture_inputs())
    receipt = publish(database, candidate, str(uuid4()), return_surface=False)
    def no_json(*args):
        pytest.fail("Internal persisted readback serialized its frame")
    monkeypatch.setattr(service, "_surface_frame_json", no_json)
    verified = api.verify_daily_readback(database, candidate, receipt["publication_id"])
    assert verified["point_count"] == len(candidate["surface"])


def test_set_based_history_hash_matches_original_full_row_encoding(database):
    candidate = api.build_settlement_candidate(fixture_inputs())
    receipt = publish(database, candidate, str(uuid4()), return_surface=False)
    with database.connect() as c:
        original = c.execute(text("""SELECT
            (SELECT encode(sha256(convert_to(COALESCE(string_agg(to_jsonb(s)::text,E'\\n'
              ORDER BY s.contract_date,s.strike),''),'UTF8')),'hex')
             FROM at_lng.implied_volatility_surface_calibrated s WHERE s.publication_id=p.publication_id) points,
            (SELECT encode(sha256(convert_to(COALESCE(string_agg(to_jsonb(e)::text,E'\\n'
              ORDER BY e.option_expiration_date),''),'UTF8')),'hex')
             FROM at_lng.vol_calibration_expiry_results e WHERE e.run_id=p.run_id) results,
            encode(sha256(convert_to(r.configuration::text,'UTF8')),'hex') configuration
            FROM at_lng.vol_surface_publications p JOIN at_lng.vol_calibration_runs r USING(run_id)
            WHERE p.publication_id=CAST(:id AS uuid)"""), {"id":receipt["publication_id"]}).mappings().one()
    assert api.historical_content_snapshot(database) == {receipt["publication_id"]: dict(original)}
    assert api.historical_content_snapshot(database, []) == {}




def test_compact_receipt_header_audit_and_full_precision_readback(database):
    candidate = api.build_settlement_candidate(fixture_inputs())
    key = str(uuid4())
    timings = []
    with api.calibration_trace(timings.append):
        receipt = publish(database, candidate, key, return_surface=False)
    assert receipt["data"] is None and not receipt["surface_loaded"]
    assert {"publication_lock", "database_checkout", "surface_copy", "transaction_commit"} <= {
        r["stage"] for r in timings
    }
    assert all(r["seconds"] >= 0 for r in timings)
    queries = []

    def record(conn, cursor, statement, parameters, context, many):
        queries.append(statement)

    event.listen(database, "before_cursor_execute", record)
    header = api.load_publication_header(database, "2026-10-02", commodity="BRENT", require_exact_cob=True)
    event.remove(database, "before_cursor_execute", record)
    assert header["publication_id"] == receipt["publication_id"]
    assert header["data"] is None and header["expiry_results"] == []
    assert not any(
        "FROM at_lng.implied_volatility_surface_calibrated" in q or "FROM at_lng.vol_calibration_expiry_results" in q
        for q in queries
    )
    assert any("jsonb_build_object" in q for q in queries)
    assert (
        api.load_publication_header(database, "2026-10-01", commodity="BRENT", require_exact_cob=True)["publication_id"]
        is None
    )
    complete = api.load_latest_hybrid_publication(database, "2026-10-02", commodity="BRENT", require_exact_cob=True)
    actual = api.hybrid_publication_frame(complete).sort_values(["contract_date", "strike"])
    expected = candidate["surface"].sort_values(["contract_date", "strike"])
    for column in ["strike", "delta", "volatility", "total_variance", "working_forward"]:
        np.testing.assert_array_equal(actual[column], expected[column])
    configuration = api.load_publication_configuration(database, receipt["publication_id"], commodity="BRENT")
    with database.connect() as connection:
        audits = (
            connection.execute(
                text("SELECT event_type,details FROM at_lng.vol_calibration_audit_events WHERE run_id=:run"),
                {"run": receipt["run_id"]},
            )
            .mappings()
            .all()
        )
    assert {r["event_type"] for r in audits} == {"created", "submitted", "approved", "published"}
    created = next(r["details"] for r in audits if r["event_type"] == "created")
    assert created["schema_version"] == 2 and "input_manifest" not in created
    assert created["configuration_reference"]["run_id"] == receipt["run_id"]
    assert (
        created["configuration_fingerprint"]
        == hashlib.sha256(json.dumps(configuration, sort_keys=True).encode()).hexdigest()
    )
    assert configuration["validation_certificate"] == candidate["certificate"]
    again = publish(database, candidate, key, return_surface=False)
    assert again["publication_id"] == receipt["publication_id"]
    with database.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM at_lng.vol_surface_publications")).scalar() == 1


def test_failing_stage_rolls_back_and_observability_cannot_break_commit(database):
    candidate = api.build_settlement_candidate(fixture_inputs())

    def broken_sink(_):
        raise OSError("test trace storage failure")

    with api.calibration_trace(broken_sink):
        receipt = publish(database, candidate, str(uuid4()), return_surface=False)
    assert receipt["row_count"] == len(candidate["surface"])
    timings = []

    def reject(_):
        raise ValueError("source changed")

    with api.calibration_trace(timings.append), pytest.raises(ValueError, match="source changed"):
        publish(database, candidate, str(uuid4()), return_surface=False, transaction_guard=reject)
    assert any(r["stage"] == "source_guard_before_write" and r["outcome"] == "failed" for r in timings)
    with database.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM at_lng.vol_surface_publications")).scalar() == 1


def test_first_advisory_lock_has_a_bounded_timeout(database):
    from sqlalchemy.exc import OperationalError

    candidate = api.build_settlement_candidate(fixture_inputs())
    with database.begin() as lock:
        lock.execute(text("SELECT pg_advisory_xact_lock(hashtext('BRENT:2026-10-02'))"))
        with pytest.raises(OperationalError, match="lock timeout"):
            publish(database, candidate, str(uuid4()), return_surface=False, lock_timeout_ms=50)
    with database.connect() as c:
        assert c.execute(text("SELECT count(*) FROM at_lng.vol_surface_publications")).scalar() == 0


def test_binary_digest_matches_postgres_and_retained_history_scrub(database):
    from options.vol_calibration.publication.integrity import point_content_digest, persisted_point_digest

    candidate = api.build_settlement_candidate(fixture_inputs())
    receipt = publish(database, candidate, str(uuid4()), return_surface=False)
    before = api.historical_content_snapshot(database)
    with database.connect() as c:
        rows = (
            c.execute(
                text("SELECT * FROM at_lng.implied_volatility_surface_calibrated WHERE publication_id=:id"),
                {"id": receipt["publication_id"]},
            )
            .mappings()
            .all()
        )
        assert point_content_digest(rows) == persisted_point_digest(c, receipt["publication_id"])
    assert before == api.historical_content_snapshot(database, list(before))
    changed = [dict(row) for row in rows]
    changed[0]["source_name"] = "£ gas | unicode æ"
    assert point_content_digest(changed) != point_content_digest(rows)
    changed = [dict(row) for row in rows]
    changed[0]["strike"] = np.nextafter(float(changed[0]["strike"]), float("inf"))
    assert point_content_digest(changed) != point_content_digest(rows)
