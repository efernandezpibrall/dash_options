"""Presentation of collection coverage and independent volume comparisons."""

from collections.abc import Mapping


LEGACY_VOLUME_REASON = "active trade sizes do not reconcile to date-qualified reference volume"


def tape_quality_details(metrics):
    metrics = metrics if isinstance(metrics, Mapping) else {}
    reconciliation = metrics.get("volume_reconciliation")
    reasons = [str(reason) for reason in metrics.get("coverage_reasons") or []]
    if not isinstance(reconciliation, Mapping) or not {
        "differences", "reference_unavailable"
    }.issubset(reconciliation):
        return (["tape coverage partial"] if metrics.get("coverage_status") == "partial" else []) + reasons

    details = []
    if metrics.get("collection_status"):
        details.append(f"Requested tick collection {metrics['collection_status']}")
    # New manifests carry separate collection reasons. Old immutable snapshots
    # have sufficient structured evidence to replace only the generic warning.
    collection_reasons = metrics.get("collection_reasons")
    if collection_reasons is None:
        collection_reasons = [reason for reason in reasons if reason != LEGACY_VOLUME_REASON]
    details.extend(str(reason) for reason in collection_reasons)
    unavailable = reconciliation["reference_unavailable"]
    differences = reconciliation["differences"]
    if unavailable:
        date_count = len(reconciliation.get("reference_date_unavailable") or [])
        label = ("Volume reference date unavailable" if date_count == len(unavailable)
                 else "Date-qualified volume reference unavailable")
        details.append(f"{label} for {len(unavailable):,} securities")
    if differences:
        details.append(f"Volume mismatch for {len(differences):,} securities")
    else:
        comparable_count = reconciliation.get("comparable_security_count")
        # Legacy snapshots lack a count; positive qualified volume still proves
        # comparisons took place. Never claim a match when none was possible.
        if comparable_count or reconciliation.get("reference_volume_total", 0) > 0:
            details.append("No mismatches among comparable securities")
        else:
            details.append("No date-qualified volume comparisons available")
    return details
