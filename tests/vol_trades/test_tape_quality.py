from types import SimpleNamespace

import pandas as pd

from dash_options.features.vol_trades.tape_quality import tape_quality_details, LEGACY_VOLUME_REASON
from dash_options.features.vol_trades.page import _intraday_success_message
from dash_options.features.vol_trades.chart_information import expiry_information


def metrics(**reconciliation):
    return dict(coverage_status="partial", coverage_reasons=[LEGACY_VOLUME_REASON],
                volume_reconciliation=dict(differences=[], reference_unavailable=["a"] * 97,
                                           reference_volume_total=4920, **reconciliation))


def test_legacy_snapshot_warning_uses_structured_evidence_without_rewriting_history():
    data = metrics()
    text = _intraday_success_message(SimpleNamespace(metrics=data, result_status="partial"))
    assert "reference unavailable for 97 securities" in text
    assert "No mismatches among comparable securities" in text
    assert "tape coverage partial" not in text and LEGACY_VOLUME_REASON not in text
    assert data["coverage_reasons"] == [LEGACY_VOLUME_REASON]


def test_new_manifest_separates_collection_reference_and_comparison():
    data = metrics(comparable_security_count=12, reference_date_unavailable=["a"] * 97)
    data.update(collection_status="complete", collection_reasons=[])
    text = "; ".join(tape_quality_details(data))
    assert "Requested tick collection complete" in text
    assert "Volume reference date unavailable for 97 securities" in text
    assert "No mismatches among comparable securities" in text


def test_missing_response_and_real_difference_remain_visible():
    data = metrics()
    data["coverage_reasons"].append("Bloomberg omitted option tick responses")
    data["volume_reconciliation"]["differences"] = [dict(security="b", difference=-10)]
    text = "; ".join(tape_quality_details(data))
    assert "Bloomberg omitted option tick responses" in text and "Volume mismatch for 1 securities" in text
    assert "No mismatches" not in text


def test_no_comparable_volume_does_not_claim_matches():
    data = metrics(comparable_security_count=0)
    data["volume_reconciliation"]["reference_volume_total"] = 0
    text = "; ".join(tape_quality_details(data))
    assert "No date-qualified volume comparisons available" in text
    assert "No mismatches" not in text


def test_expiry_tooltip_marks_reconciliation_as_snapshot_wide():
    tape = pd.DataFrame([dict(trade_lifecycle="active", trade_size=20,
                              coverage_status="partial", tape_metadata=metrics())])
    info = expiry_information({}, pd.DataFrame(), tape)
    assert "Snapshot: Date-qualified volume reference unavailable for 97 securities" in info["detail"]
    assert "No mismatches among comparable securities" in info["detail"]
    assert info["active_trade_lots"] == 20
