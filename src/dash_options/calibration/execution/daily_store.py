"""Durable compact artifacts and fenced stage transitions for daily jobs."""

import json
from sqlalchemy import text, bindparam
from options.vol_calibration import api
from dash_options.calibration.execution.settlement_worker import _guard


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

    def reuse_candidate(self, job, input_identity, base_publication_id):
        """Copy an owned, successful exact-identity candidate inside PostgreSQL.

        A new publish job can reuse an accepted prepare job. The receiving
        worker still decodes/certifies the immutable artifact and rechecks its
        current source, prior publication and expected activation revision.
        """
        payload = job.payload
        with self.engine.begin() as connection:
            _guard(connection, job, lock=False)
            row = connection.execute(text("""SELECT a.job_id,a.digest
                FROM at_lng.vol_daily_artifacts a JOIN at_lng.vol_calibration_jobs j USING(job_id)
                WHERE a.artifact_key='candidate' AND j.job_id<>CAST(:job AS uuid)
                  AND j.created_by=:owner AND j.status='succeeded'
                  AND j.payload->>'job_type'=:kind AND j.payload->>'product'=:product
                  AND j.payload->>'cob'=:cob AND j.payload->>'input_identity'=:identity
                  AND j.payload->>'code_identity'=:code AND j.payload->>'worker_identity'=:worker
                  AND j.payload->>'base_publication_id' IS NOT DISTINCT FROM :base
                  AND j.payload->>'warm_start_publication_id' IS NOT DISTINCT FROM :warm
                ORDER BY j.finished_at DESC,j.job_id LIMIT 1"""),
                {'job':job.job_id, 'owner':job.created_by, 'kind':payload['job_type'],
                 'product':payload['product'], 'cob':payload['cob'], 'identity':input_identity,
                 'code':payload['code_identity'], 'worker':payload['worker_identity'],
                 'base':base_publication_id,
                 'warm':payload.get('warm_start_publication_id')}).mappings().one_or_none()
            if row is None:
                return None
            connection.execute(text("""INSERT INTO at_lng.vol_daily_artifacts
                (job_id,artifact_key,codec,digest,raw_bytes,payload)
                SELECT :job,artifact_key,codec,digest,raw_bytes,payload
                FROM at_lng.vol_daily_artifacts WHERE job_id=:source AND artifact_key='candidate'
                ON CONFLICT DO NOTHING"""), {'job':job.job_id,'source':row['job_id']})
            actual = connection.execute(text("""SELECT digest FROM at_lng.vol_daily_artifacts
                WHERE job_id=:job AND artifact_key='candidate'"""), {'job':job.job_id}).scalar_one()
            if actual != row['digest']:
                raise ValueError('Retained daily candidate conflicts with the receiving job')
            self.insert(connection, job.job_id, 'candidate_reuse', api.encode_daily_artifact({
                'schema':1, 'source_job_id':str(row['job_id']), 'candidate_digest':actual,
                'input_identity':input_identity}))
            _guard(connection, job)
            return str(row['job_id'])

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
                _guard(connection, job, lock=False)
                self.insert(connection, job.job_id, key, encoded)
                _guard(connection, job)

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
        with api.calibration_stage(job.payload["product"], "checkpoint_persistence", expiries=len(records)):
            return self._save_checkpoints(job, records)

    def _save_checkpoints(self, job, records):
        prepared = [(key, api.encode_daily_artifact(value)) for key, value in records]
        with self.engine.begin() as connection:
            _guard(connection, job, lock=False)
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
            _guard(connection, job)
            connection.execute(
                text(
                    "UPDATE at_lng.vol_calibration_jobs SET completed_items=(SELECT count(*) FROM at_lng.vol_daily_artifacts WHERE job_id=:job AND artifact_key LIKE 'expiry:%') WHERE job_id=:job"
                ),
                {"job": job.job_id},
            )
