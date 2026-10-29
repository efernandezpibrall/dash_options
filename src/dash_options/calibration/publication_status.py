"""Small read-only publication projection for progress, warnings and refresh identity."""

import hashlib
import json

from sqlalchemy import text
from dash_options.calibration.execution.errors import safe_failure_reason


def committed_publication(engine, key, certificate):
    """Resolve a certified commit before either worker replays a publication."""
    with engine.connect() as connection:
        row = connection.execute(text("""SELECT p.publication_id,p.status,p.is_active,
            r.configuration->'validation_certificate'->>'certificate_identity' AS certificate
            FROM at_lng.vol_surface_publications p JOIN at_lng.vol_calibration_runs r USING(run_id)
            WHERE p.idempotency_key=:key"""), {"key": key}).mappings().one_or_none()
    if row is None:
        return None
    if row["certificate"] != certificate or row["status"] != "published" or not row["is_active"]:
        raise ValueError("Committed publication is different or superseded; review required")
    return str(row["publication_id"])


def matching_active_publication(connection, publication_id, certificate, *, product, cob, lock=False):
    """Exact current revision only; the caller owns source/lineage guards."""
    sql = """SELECT p.publication_id FROM at_lng.vol_surface_publications p
        JOIN at_lng.vol_calibration_runs r USING(run_id)
        WHERE p.publication_id=CAST(:publication AS uuid) AND p.commodity=:product
          AND p.cob_date=:cob AND p.status='published' AND p.is_active
          AND r.configuration->'validation_certificate'->>'certificate_identity'=:certificate"""
    if lock:
        sql += ' FOR SHARE OF p'
    row = connection.execute(text(sql), {'publication':str(publication_id), 'product':product,
                            'cob':cob, 'certificate':certificate}).scalar_one_or_none()
    return str(row) if row else None


def load_publication_status(engine, cob, *, owner=None):
    with engine.connect() as connection:
        publications = [dict(row) for row in connection.execute(text("""SELECT p.commodity,p.cob_date,
            p.publication_id::text,p.published_at,
            COALESCE(r.configuration#>'{validation_certificate,warnings}','[]'::jsonb) AS warnings
            FROM at_lng.vol_surface_publications p JOIN at_lng.vol_calibration_runs r USING(run_id)
            WHERE p.is_active AND p.status='published' AND p.cob_date=CAST(:cob AS date)
                AND p.commodity IN ('BRENT','HH','TTF','JKM','NBP') ORDER BY p.commodity"""),
            {"cob": cob}).mappings()]
        latest = [dict(row) for row in connection.execute(text("""SELECT DISTINCT ON (commodity)
            commodity,cob_date,publication_id::text FROM at_lng.vol_surface_publications
            WHERE is_active AND status='published' AND commodity IN ('BRENT','HH','TTF','JKM','NBP')
            ORDER BY commodity,cob_date DESC,published_at DESC""")).mappings()]
        jobs = []
        if owner and connection.execute(text("SELECT to_regclass('at_lng.vol_daily_stages')")).scalar():
            jobs = [dict(row) for row in connection.execute(text("""SELECT DISTINCT ON (s.commodity)
                s.commodity,s.stage,s.publication_id::text,s.details,j.status,j.failure_kind,
                left(j.last_error,1000) AS last_error,j.completed_items,j.total_items
                FROM at_lng.vol_daily_stages s JOIN at_lng.vol_calibration_jobs j USING(job_id)
                WHERE s.cob_date=CAST(:cob AS date) AND j.created_by=:owner
                ORDER BY s.commodity,s.updated_at DESC"""), {"cob": cob, "owner": owner}).mappings()]
    vector = {"selected": {row["commodity"]: row["publication_id"] for row in publications},
              "latest": {row["commodity"]: row["publication_id"] for row in latest}}
    return {"revision": hashlib.sha256(json.dumps(vector, sort_keys=True).encode()).hexdigest(),
            "publications": publications, "jobs": jobs,
            "latest_cob": max((str(row["cob_date"]) for row in latest), default=None)}


def status_notices(status):
    labels = {"staging": "uploading", "staged": "ready to publish", "publishing": "publishing",
              "published": "published", "verification_pending": "published; verification pending",
              "verified": "verified", "calibrating": "calibrating", "held": "held for review",
              "prepared": "inputs prepared", "waiting": "waiting to retry", "accepted": "calibrated",
              "cancelled": "cancelled"}
    messages = []
    active = {row['commodity']:row for row in status['publications']}
    for row in status["jobs"]:
        stage = row['stage']
        details = row.get('details') or {}
        publication_id = row.get('publication_id')
        planned = details.get('planned_publication_id')
        if stage == 'publishing' and planned and active.get(row['commodity'], {}).get('publication_id') == planned:
            stage, publication_id = 'verification_pending', planned
        tone = 'success' if stage in {'verified','accepted'} else 'neutral'
        label = labels.get(stage, stage)
        if stage == 'held' or row.get('status') == 'failed':
            tone = 'error'
            label = 'execution failed; audited retry available' if row.get('failure_kind') == 'retryable' else 'held for review'
        elif stage != 'verification_pending' and row.get('status') == 'queued' and row.get('failure_kind') == 'retryable':
            label, tone = 'waiting to retry', 'warning'
        if stage == 'verification_pending':
            tone = 'warning'
        message = f"{row['commodity']}: {label}"
        if publication_id:
            message += f" · publication {publication_id[:8]}"
        if stage == 'calibrating' and row.get('total_items'):
            message += f" · {row.get('completed_items',0)}/{row['total_items']} expiries"
        reason = row.get('last_error') or details.get('message')
        if reason and row.get('status') in {'failed','queued'}:
            message += ': ' + safe_failure_reason(reason)
            failures = details.get('failed_expiries') or []
            if failures:
                message += f" · {safe_failure_reason(failures[0].get('expiry',''))}: {safe_failure_reason(failures[0].get('message',''))}"
        messages.append({'message':message, 'tone':tone, 'publication_id':publication_id})
    for row in status["publications"]:
        for warning in row["warnings"]:
            if warning.get("code") == "mean_quote_error_exceeded":
                messages.append({'message':warning["message"], 'tone':'warning',
                                 'publication_id':row.get('publication_id')})
    return messages


def status_messages(status):
    return [notice['message'] for notice in status_notices(status)]
