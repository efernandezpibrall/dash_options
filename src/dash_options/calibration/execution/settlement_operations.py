"""Reviewed settlement calendars, approval evidence, recovery and persistent alerts."""
from contextlib import contextmanager
from datetime import date
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import json
import logging

import pandas as pd
from sqlalchemy import text
from options.vol_calibration import api

from dash_options.calibration.auth import Permission, authorize

LOGGER = logging.getLogger(__name__)
JOB_TYPE = "brent_hh_settlement_v1"


class ConnectionEngine:
    """Reuse the transaction which owns the source and execution locks."""
    def __init__(self, connection):
        self.connection = connection

    @contextmanager
    def connect(self):
        yield self.connection


def import_calendar(engine, manifest, *, identity):
    """Import reviewed exchange sessions; neither weekdays nor cutoff times are guessed."""
    authorize(identity, Permission.APPROVE)
    product = manifest.get("commodity")
    if manifest.get("schema") != 1 or product not in {"BRENT", "HH"}:
        raise ValueError("Calendar needs schema 1 and BRENT or HH commodity")
    if not manifest.get("version") or not manifest.get("source"):
        raise ValueError("Calendar version and source provenance are required")
    try:
        ZoneInfo(manifest["timezone"])
    except (KeyError, ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("Calendar needs a valid exchange timezone") from exc
    start, end = date.fromisoformat(manifest["from_cob"]), date.fromisoformat(manifest["through_cob"])
    if end < start or not isinstance(manifest.get("sessions"), list):
        raise ValueError("Calendar coverage and explicit sessions are required")
    calendar_id = api.settlement_identity(manifest)
    rows, seen = [], set()
    for record in manifest["sessions"]:
        cob = date.fromisoformat(record["cob"])
        ready, deadline = pd.Timestamp(record["ready_at"]), pd.Timestamp(record["deadline_at"])
        if ready.tzinfo is None or deadline.tzinfo is None or deadline <= ready:
            raise ValueError("Session ready/deadline timestamps need explicit timezones and positive ordering")
        if not start <= cob <= end or cob in seen:
            raise ValueError("Sessions must be unique within the declared coverage")
        seen.add(cob)
        rows.append({"product": product, "cob": cob, "ready": ready.to_pydatetime(),
                     "deadline": deadline.to_pydatetime(), "calendar": calendar_id,
                     "version": manifest["version"], "source": manifest["source"], "actor": identity.subject})
    with engine.begin() as connection:
        connection.execute(text("SET LOCAL lock_timeout = '5s'"))
        connection.execute(text("LOCK TABLE at_lng.vol_settlement_sessions IN SHARE ROW EXCLUSIVE MODE"))
        existing = connection.execute(text("""SELECT cob_date FROM at_lng.vol_settlement_sessions
            WHERE commodity=:product AND cob_date BETWEEN :start AND :end"""),
            {"product": product, "start": start, "end": end}).scalars().all()
        if set(existing) - seen:
            raise ValueError("Calendar would remove previously reviewed sessions")
        for row in rows:
            prior = connection.execute(text("""SELECT ready_at,deadline_at,calendar_version,calendar_source
                FROM at_lng.vol_settlement_sessions WHERE commodity=:product AND cob_date=:cob"""), row).mappings().first()
            if prior and (pd.Timestamp(prior["ready_at"]) != pd.Timestamp(row["ready"]) or
                          pd.Timestamp(prior["deadline_at"]) != pd.Timestamp(row["deadline"]) or
                          prior["calendar_version"] != row["version"] or prior["calendar_source"] != row["source"]):
                raise ValueError("Reviewed session changed; use a reviewed calendar correction migration")
            connection.execute(text("""INSERT INTO at_lng.vol_settlement_sessions
                (commodity,cob_date,ready_at,deadline_at,calendar_identity,calendar_version,calendar_source,imported_by)
                VALUES (:product,:cob,:ready,:deadline,:calendar,:version,:source,:actor)
                ON CONFLICT (commodity,cob_date) DO NOTHING"""), row)
        connection.execute(text("""INSERT INTO at_lng.vol_settlement_calendars
            (calendar_identity,commodity,from_cob,through_cob,manifest,imported_by)
            VALUES (:hash,:product,:start,:end,CAST(:manifest AS jsonb),:actor)
            ON CONFLICT (calendar_identity) DO NOTHING"""),
            {"hash": calendar_id, "product": product, "start": start, "end": end,
             "manifest": json.dumps(manifest, allow_nan=False), "actor": identity.subject})
    return calendar_id


def shadow_evidence(connection, product, *, start, through, count, worker_identity):
    """Check each required calendar session and its latest job/source, including failures."""
    coverage = connection.execute(text("""SELECT from_cob,through_cob FROM at_lng.vol_settlement_calendars
        WHERE commodity=:product AND through_cob>=:start AND from_cob<=:through ORDER BY from_cob"""),
        {"product": product, "start": start, "through": through}).all()
    frontier = pd.Timestamp(start).date()
    for lower, upper in coverage:
        if lower > frontier:
            break
        frontier = max(frontier, upper + pd.Timedelta(days=1))
    if frontier <= pd.Timestamp(through).date():
        raise ValueError("Reviewed settlement calendar does not cover the promotion window")
    rows = connection.execute(text("""WITH dates AS (
        SELECT cob_date FROM at_lng.vol_settlement_sessions WHERE commodity=:product
        UNION SELECT (payload->>'cob')::date FROM at_lng.vol_calibration_jobs
            WHERE job_type=:type AND payload->>'product'=:product AND payload->>'mode'='shadow'
        UNION SELECT business_date FROM at_lng.vol_market_snapshots WHERE commodity=:source_product
            AND status='complete' AND COALESCE(metadata->>'snapshot_kind','SETTLEMENT')='SETTLEMENT')
        SELECT d.cob_date,s.calendar_identity,s.ready_at,s.deadline_at,
            j.job_id,j.status,j.payload,j.created_at,j.finished_at,c.state,c.candidate
        FROM dates d LEFT JOIN at_lng.vol_settlement_sessions s ON s.commodity=:product AND s.cob_date=d.cob_date
        LEFT JOIN LATERAL (SELECT * FROM at_lng.vol_calibration_jobs
            WHERE job_type=:type AND payload->>'product'=:product AND payload->>'mode'='shadow'
            AND (payload->>'cob')::date=d.cob_date ORDER BY created_at DESC,job_id DESC LIMIT 1) j ON true
        LEFT JOIN at_lng.vol_settlement_candidates c USING(job_id)
        WHERE d.cob_date BETWEEN :start AND :through ORDER BY d.cob_date DESC LIMIT :count"""),
        {"product": product, "source_product": "LNE" if product == "HH" else "BRENT", "type": JOB_TYPE,
         "start": start, "through": through, "count": count}).mappings().all()
    if len(rows) < count:
        raise ValueError(f"{product} needs {count} prospective shadow sessions")
    evidence = []
    code = api.implementation_fingerprint()
    for row in rows:
        if not row["calendar_identity"] or row["status"] != "succeeded" or row["state"] != "shadow":
            raise ValueError(f"{product} {row['cob_date']} required shadow session is missing, pending or failed")
        if (row["finished_at"] is None or not row["ready_at"] <= row["created_at"] <= row["finished_at"] <= row["deadline_at"]):
            raise ValueError(f"{product} {row['cob_date']} shadow session missed its readiness/deadline window")
    for row in rows:
        payload = row["payload"] or {}
        api.assert_current_settlement_source(ConnectionEngine(connection), product, row["cob_date"], payload.get("snapshot_id"))
        candidate = api.unpack_settlement(row["candidate"])
        cert = candidate["certificate"]
        if payload.get("worker_identity") != worker_identity or cert.get("code_identity") != code:
            raise ValueError("Shadow evidence uses a different implementation")
        if cert.get("cob") != row["cob_date"].isoformat() or cert.get("product") != product or cert.get("input_identity") != payload.get("input_identity"):
            raise ValueError("Shadow evidence does not match its job and settlement")
        api.verify_settlement_certificate(candidate["surface"], candidate["results"], candidate["manifest"], cert)
        evidence.append({"cob": row["cob_date"].isoformat(), "job_id": str(row["job_id"]),
                         "calendar": row["calendar_identity"], "certificate": cert["certificate_identity"]})
    return api.settlement_identity(evidence)


def approve_promotion(engine, product, through, *, identity, worker_identity):
    authorize(identity, Permission.APPROVE)
    through = pd.Timestamp(through).date()
    with engine.begin() as connection:
        gate = connection.execute(text("SELECT * FROM at_lng.vol_settlement_promotion WHERE commodity=:product FOR UPDATE"),
                                  {"product": product}).mappings().one()
        connection.execute(text("SET LOCAL lock_timeout = '5s'"))
        connection.execute(text("LOCK TABLE at_lng.vol_market_snapshots IN SHARE MODE"))
        evidence = shadow_evidence(connection, product, start=gate["shadow_start_cob"], through=through,
                                   count=gate["required_sessions"], worker_identity=worker_identity)
        connection.execute(text("""UPDATE at_lng.vol_settlement_promotion SET approved=true,approved_by=:actor,
            approved_at=clock_timestamp(),evidence_through_cob=:through,evidence_identity=:evidence
            WHERE commodity=:product"""), {"product": product, "actor": identity.subject,
                                          "through": through, "evidence": evidence})
    return evidence


def retry_settlement(engine, job_id, *, identity, reason, worker_identity):
    authorize(identity, Permission.SUBMIT)
    if not str(reason).strip():
        raise ValueError("An audited retry reason is required")
    with engine.begin() as connection:
        job = connection.execute(text("SELECT * FROM at_lng.vol_calibration_jobs WHERE job_id=:id FOR UPDATE"),
                                 {"id": job_id}).mappings().one()
        if job["job_type"] != JOB_TYPE or job["status"] != "failed" or job["failure_kind"] != "retryable":
            raise ValueError("Only failed settlement jobs with a retryable execution failure may be retried")
        payload = job["payload"]
        if payload.get("worker_identity") != worker_identity or payload.get("code_identity") != api.implementation_fingerprint():
            raise ValueError("Implementation changed; submit a new settlement job")
        api.assert_current_settlement_source(ConnectionEngine(connection), payload["product"], payload["cob"], payload["snapshot_id"], lock=True)
        inputs = api.load_settlement_inputs(ConnectionEngine(connection), payload["product"], payload["cob"], payload["snapshot_id"])
        if inputs.identity != payload["input_identity"]:
            raise ValueError("Inputs changed; submit a new settlement job")
        maximum = job["max_attempts"] + 3
        connection.execute(text("""INSERT INTO at_lng.vol_settlement_retry_audit
            (job_id,actor,reason,prior_attempts,prior_max_attempts,new_max_attempts)
            VALUES (:id,:actor,:reason,:attempts,:prior,:maximum)"""),
            {"id": job_id, "actor": identity.subject, "reason": reason, "attempts": job["attempts"],
             "prior": job["max_attempts"], "maximum": maximum})
        connection.execute(text("""UPDATE at_lng.vol_calibration_jobs SET status='queued',max_attempts=:maximum,
            next_attempt_at=clock_timestamp(),finished_at=NULL,updated_at=clock_timestamp()
            WHERE job_id=:id"""), {"id": job_id, "maximum": maximum})
    return str(job_id)


def require_session(connection, product, cob):
    session = connection.execute(text("""SELECT * FROM at_lng.vol_settlement_sessions
        WHERE commodity=:product AND cob_date=:cob FOR SHARE"""), {"product": product, "cob": cob}).mappings().first()
    now = connection.execute(text("SELECT clock_timestamp()")).scalar_one()
    if session is None:
        raise ValueError("Automatic publication requires a reviewed settlement session")
    if not session["ready_at"] <= now <= session["deadline_at"]:
        raise ValueError("Settlement publication is outside its reviewed readiness/deadline window")


def refresh_alerts(engine, *, from_cob, mode):
    """Persist actionable state and log only new/changed alerts and resolutions."""
    with engine.begin() as connection:
        rows = connection.execute(text("""SELECT s.commodity,s.cob_date,s.ready_at,s.deadline_at,
            j.status,j.failure_kind,j.created_at,j.finished_at,
            j.last_error,c.state,c.publication_id,clock_timestamp()>s.deadline_at AS overdue,
            snap.snapshot_id,(j.payload->>'snapshot_id') AS job_snapshot_id
            FROM at_lng.vol_settlement_sessions s
            LEFT JOIN LATERAL (SELECT snapshot_id FROM at_lng.vol_market_snapshots
                WHERE commodity=CASE WHEN s.commodity='HH' THEN 'LNE' ELSE s.commodity END
                AND business_date=s.cob_date AND status='complete'
                AND COALESCE(metadata->>'snapshot_kind','SETTLEMENT')='SETTLEMENT'
                ORDER BY observed_at DESC,created_at DESC,snapshot_id DESC LIMIT 1) snap ON true
            LEFT JOIN LATERAL (SELECT * FROM at_lng.vol_calibration_jobs WHERE job_type=:type
                AND payload->>'product'=s.commodity AND (payload->>'cob')::date=s.cob_date
                AND payload->>'mode'=:mode ORDER BY created_at DESC,job_id DESC LIMIT 1) j ON true
            LEFT JOIN at_lng.vol_settlement_candidates c USING(job_id)
            WHERE s.cob_date>=:start AND s.ready_at<=clock_timestamp()"""),
            {"type": JOB_TYPE, "mode": mode, "start": pd.Timestamp(from_cob).date()}).mappings().all()
        active = set()
        calendars = connection.execute(text("""SELECT DISTINCT ON (commodity) commodity,through_cob,manifest
            FROM at_lng.vol_settlement_calendars ORDER BY commodity,through_cob DESC,imported_at DESC""")).mappings().all()
        for product in ("BRENT", "HH"):
            calendar = next((row for row in calendars if row["commodity"] == product), None)
            today = pd.Timestamp.now(tz=calendar["manifest"]["timezone"] if calendar else "UTC").date()
            if calendar is None or today > calendar["through_cob"]:
                kind = "calendar_missing" if calendar is None else "calendar_expired"
                key = f"{mode}:{product}:calendar"
                active.add(key)
                details = {"mode": mode, "kind": kind,
                           "through_cob": calendar["through_cob"].isoformat() if calendar else None}
                _upsert_alert(connection, key, product, today, kind, details)
        for row in rows:
            kind = None
            if row["status"] in {"failed", "cancelled"}:
                kind = "execution_failed" if row["failure_kind"] == "retryable" else "settlement_held"
            elif row["status"] == "succeeded" and row["state"] != ("shadow" if mode == "shadow" else "published"):
                kind = "completion_inconsistent"
            elif row["status"] == "succeeded" and (row["finished_at"] is None or
                    not row["ready_at"] <= row["created_at"] <= row["finished_at"] <= row["deadline_at"]):
                kind = "deadline_missed"
            elif row["snapshot_id"] and str(row["snapshot_id"]) != row["job_snapshot_id"] and row["status"] == "succeeded":
                kind = "source_correction"
            elif row["overdue"] and row["status"] != "succeeded":
                kind = "deadline_missed"
            if kind is None:
                continue
            key = f"{mode}:{row['commodity']}:{row['cob_date']}:{kind}"
            active.add(key)
            details = {"mode": mode, "status": row["status"], "error": row["last_error"],
                       "deadline": row["deadline_at"].isoformat()}
            _upsert_alert(connection, key, row["commodity"], row["cob_date"], kind, details)
        prior = connection.execute(text("""SELECT alert_key FROM at_lng.vol_settlement_alerts
            WHERE is_active AND cob_date>=:start AND details->>'mode'=:mode"""),
            {"start": pd.Timestamp(from_cob).date(), "mode": mode}).scalars().all()
        for key in set(prior) - active:
            connection.execute(text("""UPDATE at_lng.vol_settlement_alerts SET is_active=false,
                resolved_at=clock_timestamp(),updated_at=clock_timestamp() WHERE alert_key=:key"""), {"key": key})
            LOGGER.info("Settlement alert resolved: %s", key)
    return len(active)


def _upsert_alert(connection, key, product, cob, kind, details):
    changed = connection.execute(text("""INSERT INTO at_lng.vol_settlement_alerts
        (alert_key,commodity,cob_date,kind,details) VALUES (:key,:product,:cob,:kind,CAST(:details AS jsonb))
        ON CONFLICT (alert_key) DO UPDATE SET details=EXCLUDED.details,is_active=true,
        resolved_at=NULL,updated_at=clock_timestamp()
        WHERE NOT vol_settlement_alerts.is_active OR vol_settlement_alerts.details<>EXCLUDED.details
        RETURNING alert_key"""), {"key": key, "product": product, "cob": cob,
                                   "kind": kind, "details": json.dumps(details)}).scalar_one_or_none()
    if changed:
        LOGGER.error("Settlement alert %s: %s", key, details)
