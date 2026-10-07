"""SQLite와 실제 격리 MySQL의 영속·원자적 idempotency 의미를 동일하게 검증한다."""

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest

from app.core.analysis_versions import analysis_versions
from app.repositories.analysis_jobs import SQLiteJobStore
from app.repositories.idempotency import IdempotencyConflict, request_hashes
from app.repositories.mysql_jobs import MySQLJobStore


@pytest.fixture(params=["sqlite", "mysql"])
def store(request, tmp_path):
    if request.param == "mysql":
        if os.getenv("RUN_MYSQL_TESTS") != "1":
            pytest.skip("Requires explicitly configured disposable MySQL")
        db = MySQLJobStore.from_env()
    else:
        db = SQLiteJobStore(str(tmp_path / "idempotency.db"))
    db.initialize()
    keys = []
    yield db, keys
    if request.param == "mysql":
        # Only this test's UUID-prefixed records, never tables/volumes or unrelated jobs.
        with db._connection() as connection, connection.cursor() as cursor:
            for key in keys:
                key_hash, _ = request_hashes(key, {})
                cursor.execute("SELECT job_id FROM ai_analysis_idempotency WHERE key_hash=%s", (key_hash,))
                row = cursor.fetchone()
                if row:
                    cursor.execute("DELETE FROM ai_analysis_idempotency WHERE key_hash=%s", (key_hash,))
                    cursor.execute("DELETE FROM ai_analysis_jobs WHERE job_id=%s", (row["job_id"],))


def new_key(keys):
    key = "isolated-idempotency-test-" + str(uuid4())
    keys.append(key)
    return key


def test_states_and_canonical_hash(store):
    db, keys = store
    key = new_key(keys)
    claim = db.claim_idempotent(key, {"a": 1, "b": [2, 3]}, analysis_versions())
    assert claim.is_new and db.get(claim.job_id)["status"] == "RUNNING"
    with pytest.raises(IdempotencyConflict, match="IDEMPOTENCY_IN_PROGRESS"):
        db.claim_idempotent(key, {"b": [2, 3], "a": 1}, analysis_versions())
    with pytest.raises(IdempotencyConflict, match="IDEMPOTENCY_KEY_REUSED"):
        db.claim_idempotent(key, {"a": 1, "b": [3, 2]}, analysis_versions())
    db.complete(claim.job_id, {"stored": True})
    replay = db.claim_idempotent(key, {"b": [2, 3], "a": 1}, {"policy_version": "future"})
    assert replay.job_id == claim.job_id and not replay.is_new
    assert replay.versions == analysis_versions()


def test_failed_is_not_reexecuted(store):
    db, keys = store
    key = new_key(keys)
    claim = db.claim_idempotent(key, {}, analysis_versions())
    db.fail(claim.job_id, "TEST_FAILURE")
    with pytest.raises(IdempotencyConflict, match="IDEMPOTENCY_FAILED"):
        db.claim_idempotent(key, {}, analysis_versions())


def test_api_persists_and_replays(store, monkeypatch, model_free_prediction):
    from fastapi.testclient import TestClient
    from app.factory import create_app
    from tests.test_data_analysis_stream import decode
    from tests.test_data_ai_v05 import payload
    db, keys = store
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setenv("ENABLE_EXPERIMENTAL_COLLECTION", "0")
    monkeypatch.setenv("ENABLE_GROQ_REASON_NATURALIZATION", "0")
    monkeypatch.setenv("REQUIRE_INTERNAL_TOKEN", "0")
    monkeypatch.delenv("INTERNAL_TOKEN", raising=False)
    headers = {"X-Request-ID": "storage-test", "Idempotency-Key": new_key(keys)}
    with TestClient(create_app(job_store=db)) as client:
        first = client.post("/api/v1/data/analyze/stream", json=payload(), headers=headers)
        assert first.status_code == 200
        events = decode(first.text)
        assert events[-1][0] == "done"
        saved = db.get(first.headers["x-analysis-job-id"])
        result = next(data for name, data in events if name == "result")
        result.pop("request_id")
        assert saved["status"] == "DONE" and saved["result"]["results"] == [result]

        def forbidden(*args):
            pytest.fail("Replay invoked analyzer")

        monkeypatch.setattr("app.services.data_analysis_stream.evaluate_data_and_store", forbidden)
        replay = client.post("/api/v1/data/analyze/stream", json=payload(), headers=headers)
        assert replay.headers["x-analysis-job-id"] == first.headers["x-analysis-job-id"]
        assert [name for name, _ in decode(replay.text)] == ["meta", "result", "done"]


def test_concurrent_claim_exactly_one_winner(store):
    db, keys = store
    key = new_key(keys)
    barrier = Barrier(8)

    def claim(_):
        barrier.wait(timeout=10)
        try:
            return db.claim_idempotent(key, {"test_marker": key}, analysis_versions())
        except IdempotencyConflict as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(claim, range(8)))
    winners = [c for c in claims if not isinstance(c, str)]
    assert len(winners) == 1
    assert claims.count("IDEMPOTENCY_IN_PROGRESS") == 7
    if isinstance(db, MySQLJobStore):
        with db._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) AS count FROM ai_analysis_jobs WHERE JSON_UNQUOTE(JSON_EXTRACT(request_json, '$.test_marker'))=%s", (key,))
            assert cursor.fetchone()["count"] == 1
    else:
        with db._connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM ai_analysis_jobs").fetchone()[0] == 1


def test_separate_process_can_replay_sqlite(tmp_path):
    path = str(tmp_path / "process.db")
    db = SQLiteJobStore(path)
    db.initialize()
    claim = db.claim_idempotent("process-key", {}, analysis_versions())
    db.complete(claim.job_id, {"persisted": True})
    code = ("import sys; from app.repositories.analysis_jobs import SQLiteJobStore; "
            "s=SQLiteJobStore(sys.argv[1]); c=s.claim_idempotent('process-key', {}, {}); "
            "assert not c.is_new; assert s.get(c.job_id)['result']=={'persisted': True}; print(c.job_id)")
    result = subprocess.run([sys.executable, "-c", code, path], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == claim.job_id
