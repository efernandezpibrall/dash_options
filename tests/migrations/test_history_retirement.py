import hashlib
import json

import pytest
from sqlalchemy import text

from scripts.maintenance.retire_brent_history import TABLES, fingerprints, retire


def prepare(engine, tmp_path):
    with engine.begin() as db:
        db.execute(text("CREATE SCHEMA at_lng"))
        for table in TABLES:
            db.execute(text("CREATE TABLE at_lng." + table + " (id int PRIMARY KEY)"))
            db.execute(text("INSERT INTO at_lng." + table + " VALUES(1)"))
        proof = fingerprints(db)
    (tmp_path / "brent_history_restore.dump").write_bytes(b"test dump")
    (tmp_path / "restore_rehearsal.json").write_text(
        json.dumps({"status": "passed", "dump_sha256": hashlib.sha256(b"test dump").hexdigest(), "tables": proof})
    )


def present(engine):
    with engine.connect() as db:
        return [db.execute(text("SELECT to_regclass(:t)"), {"t": "at_lng." + t}).scalar() for t in TABLES]


def test_retirement_refuses_changed_data(blank_database, tmp_path):
    prepare(blank_database, tmp_path)
    with blank_database.begin() as db:
        db.execute(text("INSERT INTO at_lng.brent_delta_history_points VALUES(2)"))
    with pytest.raises(ValueError, match="changed since backup"):
        retire(blank_database, tmp_path)
    assert all(present(blank_database))


def test_unexpected_dependency_rolls_back_every_drop(blank_database, tmp_path):
    prepare(blank_database, tmp_path)
    with blank_database.begin() as db:
        db.execute(text("CREATE TABLE at_lng.other_reader(id int REFERENCES at_lng.brent_delta_history_runs(id))"))
    with pytest.raises(Exception, match="depend"):
        retire(blank_database, tmp_path)
    assert all(present(blank_database))
