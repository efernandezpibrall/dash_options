"""Durable compact artifacts and fenced stage transitions for daily jobs."""

import json
from sqlalchemy import text, bindparam
from options.vol_calibration import api
from .settlement_worker import _guard


class DailyStore:
    def __init__(self, engine):
        self.engine = engine

    def get(self, job_id, key):
        with self.engine.connect() as connection:
            record = (
                connection.execute(
                    text(
                        "SELECT codec,digest,raw_bytes,payload FROM at_lng.vol_daily_artifacts WHERE job_id=:job AND artifact_key=:key"
                    ),
                    {"job": job_id, "key": key},
                )
                .mappings()
                .one_or_none()
            )
        return api.decode_daily_artifact(record) if record else None

    @staticmethod
    def insert(connection, job_id, key, encoded):
        connection.execute(
            text("""INSERT INTO at_lng.vol_daily_artifacts(job_id,artifact_key,codec,digest,raw_bytes,payload)
            VALUES (:job,:key,:codec,:digest,:raw_bytes,:payload) ON CONFLICT DO NOTHING"""),
            {"job": job_id, "key": key, **encoded},
        )
        identity = connection.execute(
            text("SELECT digest FROM at_lng.vol_daily_artifacts WHERE job_id=:job AND artifact_key=:key"),
            {"job": job_id, "key": key},
        ).scalar_one()
        if identity != encoded["digest"]:
            raise ValueError("A daily artifact key belongs to different immutable content")

    def put(self, job, key, value):
        with api.calibration_stage(job.payload["product"], "artifact_compression", artifact=key):
            encoded = api.encode_daily_artifact(value)
        with api.calibration_stage(
            job.payload["product"],
            "artifact_write",
            artifact=key,
            raw_bytes=encoded["raw_bytes"],
            compressed_bytes=len(encoded["payload"]),
        ):
            with self.engine.begin() as connection:
                _guard(connection, job)
                self.insert(connection, job.job_id, key, encoded)

    def transition(self, job, stage, *, publication_id=None, details=None):
        with self.engine.begin() as connection:
            _guard(connection, job)
            connection.execute(
                text("""INSERT INTO at_lng.vol_daily_stages(job_id,commodity,cob_date,stage,publication_id,details)
                VALUES (:job,:product,:cob,:stage,:publication,CAST(:details AS jsonb))
                ON CONFLICT(job_id) DO UPDATE SET stage=EXCLUDED.stage,
                  publication_id=COALESCE(EXCLUDED.publication_id,vol_daily_stages.publication_id),
                  details=EXCLUDED.details,updated_at=clock_timestamp()"""),
                {
                    "job": job.job_id,
                    "product": job.payload["product"],
                    "cob": job.payload["cob"],
                    "stage": stage,
                    "publication": publication_id,
                    "details": json.dumps(details or {}, allow_nan=False),
                },
            )

    def status(self, job_id):
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    text(
                        "SELECT commodity,cob_date,stage,publication_id,details,updated_at FROM at_lng.vol_daily_stages WHERE job_id=:job"
                    ),
                    {"job": job_id},
                )
                .mappings()
                .one_or_none()
            )
        return dict(row) if row else None

    def checkpoints(self, job_id):
        with self.engine.connect() as connection:
            rows = (
                connection.execute(
                    text(
                        "SELECT artifact_key,codec,digest,raw_bytes,payload FROM at_lng.vol_daily_artifacts WHERE job_id=:job AND artifact_key LIKE 'expiry:%' ORDER BY artifact_key"
                    ),
                    {"job": job_id},
                )
                .mappings()
                .all()
            )
        return {row["artifact_key"].split(":", 1)[1]: api.decode_daily_artifact(row) for row in rows}

    def save_checkpoints(self, job, records):
        """Flush an exact bounded chunk under one fence/transaction, not per fit."""
        prepared = [(key, api.encode_daily_artifact(value)) for key, value in records]
        with self.engine.begin() as connection:
            _guard(connection, job)
            if not prepared:
                return
            from psycopg2.extras import execute_values

            with connection.connection.driver_connection.cursor() as cursor:
                execute_values(
                    cursor,
                    """INSERT INTO at_lng.vol_daily_artifacts
                    (job_id,artifact_key,codec,digest,raw_bytes,payload) VALUES %s
                    ON CONFLICT DO NOTHING""",
                    [
                        (
                            str(job.job_id),
                            "expiry:" + key,
                            item["codec"],
                            item["digest"],
                            item["raw_bytes"],
                            item["payload"],
                        )
                        for key, item in prepared
                    ],
                    page_size=len(prepared),
                )
            actual = dict(
                connection.execute(
                    text("""SELECT artifact_key,digest FROM at_lng.vol_daily_artifacts
                WHERE job_id=:job AND artifact_key IN :keys""").bindparams(bindparam("keys", expanding=True)),
                    {"job": job.job_id, "keys": ["expiry:" + key for key, _ in prepared]},
                ).all()
            )
            if any(actual.get("expiry:" + key) != item["digest"] for key, item in prepared):
                raise ValueError("Checkpoint key belongs to different immutable content")
            connection.execute(
                text(
                    "UPDATE at_lng.vol_calibration_jobs SET completed_items=(SELECT count(*) FROM at_lng.vol_daily_artifacts WHERE job_id=:job AND artifact_key LIKE 'expiry:%') WHERE job_id=:job"
                ),
                {"job": job.job_id},
            )
