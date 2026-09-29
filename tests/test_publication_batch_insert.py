"""Governed publication preserves every expiry result in one batch."""

import json
from datetime import date, datetime, timezone
from types import SimpleNamespace

import psycopg2.extras
import pytest

from vol_calibration import ttf_publication as publication


def test_storage_capability_uses_one_schema_probe(monkeypatch):
    calls = []

    def inspector(_engine):
        return SimpleNamespace(get_table_names=lambda schema: (
            calls.append(schema) or [
                "vol_calibration_runs",
                "vol_calibration_expiry_results",
                "vol_calibration_audit_events",
                "vol_surface_publications",
                "implied_volatility_surface_calibrated",
            ]
        ))

    monkeypatch.setattr(publication, "inspect", inspector)
    assert publication.ttf_publication_storage_available(object())
    assert calls == ["at_lng"]


def test_expiry_results_use_one_postgres_batch_with_complete_json(monkeypatch):
    captured = []
    cursor = SimpleNamespace(mogrify=lambda *_args: b"", close=lambda: None)
    connection = SimpleNamespace(
        connection=SimpleNamespace(
            driver_connection=SimpleNamespace(cursor=lambda: cursor)
        )
    )

    def execute_values(actual_cursor, sql, values, *, template, page_size):
        captured.append((actual_cursor, sql, values, template, page_size))

    monkeypatch.setattr(psycopg2.extras, "execute_values", execute_values)
    results = [
        {
            "option_expiration_date": "2026-10-26",
            "parameters": {"vr": 0.3},
            "diagnostics": {"point_count": 401},
            "validation": {"is_valid": True},
            "weighted_rmse": 0.001,
            "unweighted_rmse": 0.002,
            "max_error": 0.003,
            "optimizer_success": True,
        },
        {
            "option_expiration_date": "2026-11-25",
            "parameters": {"vr": 0.4},
            "diagnostics": {"point_count": 401},
            "validation": {"is_valid": True},
            "weighted_rmse": None,
            "unweighted_rmse": None,
            "max_error": None,
            "optimizer_success": False,
        },
    ]

    publication._insert_expiry_results(connection, "run-id", results)

    assert len(captured) == 1
    actual_cursor, sql, values, template, page_size = captured[0]
    assert actual_cursor is cursor
    assert publication.RESULT_TABLE in sql
    assert "%s::jsonb" in template
    assert len(values) == page_size == 2
    assert values[0][0] == "run-id"
    assert values[0][1].isoformat() == "2026-10-26"
    assert json.loads(values[0][2]) == results[0]["parameters"]
    assert json.loads(values[0][3]) == results[0]["diagnostics"]
    assert json.loads(values[0][4]) == results[0]["validation"]
    assert values[1][-1] is False


def test_compact_receipt_checks_committed_points_and_results():
    row = {
        "publication_id": "pub-id",
        "run_id": "run-id",
        "cob_date": date(2026, 9, 25),
        "published_at": datetime(2026, 9, 28, tzinfo=timezone.utc),
        "published_by": "operator",
        "input_manifest_fingerprint": "fingerprint",
        "calibration_method": "PCHIP-core/Wing-v2-tail",
        "calibration_policy_version": "ttf-pchip-core-wing-v2-tail",
        "row_count": 24862,
        "expiry_count": 62,
        "bad_point_count": 0,
        "min_fingerprint": "fingerprint",
        "max_fingerprint": "fingerprint",
        "min_points_per_month": 401,
        "max_points_per_month": 401,
        "result_count": 62,
        "all_valid": True,
    }

    class Connection:
        def execute(self, statement, parameters):
            assert "COUNT(DISTINCT contract_date)" in str(statement)
            assert "BOOL_AND" in str(statement)
            assert parameters == {"publication_id": "pub-id", "commodity": "TTF"}
            return SimpleNamespace(mappings=lambda: SimpleNamespace(
                one_or_none=lambda: row
            ))

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    engine = SimpleNamespace(connect=Connection)
    receipt = publication._load_persisted_publication_receipt(
        engine, "pub-id", commodity="TTF"
    )
    assert receipt["row_count"] == 24862
    assert receipt["result_count"] == 62
    assert receipt["surface_loaded"] is False
    assert receipt["data"] is None

    row["min_points_per_month"] = 400
    with pytest.raises(publication.TTFPublicationError, match="quality checks"):
        publication._load_persisted_publication_receipt(
            engine, "pub-id", commodity="TTF"
        )
