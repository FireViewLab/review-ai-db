"""Explanation metadata is atomic, independent of scores and absent from v0.5 wire JSON."""
from contextlib import closing
from copy import deepcopy
import json
import sqlite3
from unittest.mock import Mock

from fastapi.testclient import TestClient
import pytest

from app.contracts.data_ai_v05 import DataAnalyzeRequestV05
from app.core import analysis_versions as versions
from app.factory import create_app
from app.repositories.analysis_jobs import SQLiteJobStore
from app.services import data_analysis
from app.services.analysis import analyze_reviews
from tests.test_data_analysis_stream import decode
from tests.test_idempotency_store import store, new_key  # Shared SQLite / explicitly enabled MySQL fixture.
from tests.test_job_migrations import isolated_schema


def body():
    return dict(platform="mall", product_id="explanation-metadata", reviews=[
        dict(review_id="neutral", content="이 크림을 한 달 사용하니 세안 후 당김이 줄고 촉촉해서 만족합니다."),
        dict(review_id="coded", content="후기"),
    ])


@pytest.fixture
def api(store, monkeypatch):
    db, keys = store
    for key, value in dict(PYTHON_DOTENV_DISABLED="1", ENABLE_EXPERIMENTAL_COLLECTION="0",
                           ENABLE_GROQ_REASON_NATURALIZATION="0", REQUIRE_INTERNAL_TOKEN="0").items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("INTERNAL_TOKEN", raising=False)
    ids = []
    with TestClient(create_app(job_store=db)) as http:
        yield http, db, keys, ids
    # Only jobs created by these tests; the shared fixture handles idempotency-owned jobs.
    if not isinstance(db, SQLiteJobStore):
        for job_id in ids:
            db._execute("DELETE FROM ai_analysis_jobs WHERE job_id=%s", (job_id,))


def without_reasons(result):
    result = deepcopy(result)
    for row in result["results"]:
        row.pop("reasons")
    return result


def test_explanation_version_does_not_expand_sse_version_contract(monkeypatch):
    original = versions.analysis_versions()
    assert original == dict(contract_version="v0.5", model_version="ptext-koelectra-v1-2epoch-20260929",
                            policy_version="rti-v0.1")
    monkeypatch.setattr(versions, "EXPLANATION_POLICY_VERSION", "neutral-summary-v2-test")
    assert versions.explanation_policy_version() == "neutral-summary-v2-test"
    assert versions.analysis_versions() == original


@pytest.mark.parametrize("stream", [False, True])
def test_new_json_and_sse_persist_whole_pipeline_version(api, model_free_prediction, stream):
    http, db, keys, ids = api
    payload = body()
    raw = analyze_reviews(**payload)
    assert raw["results"][0]["reasons"] == []
    assert raw["results"][1]["reasons"]
    headers = {"X-Request-ID": "explanation-meta", "Idempotency-Key": new_key(keys)}
    response = http.post("/api/v1/data/analyze" + ("/stream" if stream else ""),
                         json=payload, headers=headers)
    assert response.status_code == 200
    job_id = response.headers["X-Analysis-Job-ID"]
    if not stream:
        ids.append(job_id)
    saved = db.get(job_id)
    assert saved["status"] == "DONE"
    assert saved["explanation_policy_version"] == "neutral-summary-v1"
    assert without_reasons(saved["result"]) == without_reasons(raw)
    assert "explanation_policy_version" not in saved["result"]
    if stream:
        events = decode(response.text)
        meta = events[0][1]
        assert set(meta) == {"request_id", "ai_job_id", "platform", "product_id", "review_count",
                             "contract_version", "model_version", "policy_version"}
        result_rows = [{k: v for k, v in row.items() if k != "request_id"}
                       for name, row in events if name == "result"]
        assert saved["result"]["results"] == result_rows
        assert set(events[-1][1]) == {"request_id", "ai_job_id", "result_count"}
    else:
        assert saved["result"] == response.json()
    assert all("explanation_policy_version" not in row for row in saved["result"]["results"])


def test_version_only_change_never_changes_scores_or_result_json(api, model_free_prediction, monkeypatch):
    http, db, _, ids = api
    first = http.post("/api/v1/data/analyze", json=body())
    assert first.status_code == 200
    ids.append(first.headers["X-Analysis-Job-ID"])
    monkeypatch.setattr(versions, "EXPLANATION_POLICY_VERSION", "neutral-summary-v2-test")
    second = http.post("/api/v1/data/analyze", json=body())
    assert second.status_code == 200
    ids.append(second.headers["X-Analysis-Job-ID"])
    assert first.json() == second.json()
    assert db.get(ids[0])["explanation_policy_version"] == "neutral-summary-v1"
    assert db.get(ids[1])["explanation_policy_version"] == "neutral-summary-v2-test"


@pytest.mark.parametrize("historical_version", [None, "neutral-summary-v0-test"])
def test_done_replay_preserves_historical_null_or_version(api, model_free_prediction, monkeypatch, historical_version):
    http, db, keys, _ = api
    payload = DataAnalyzeRequestV05.model_validate(body())
    key = new_key(keys)
    claim = db.claim_idempotent(key, payload.model_dump(mode="json"), versions.analysis_versions())
    old = analyze_reviews(**body())
    db.complete(claim.job_id, old, explanation_policy_version=historical_version)
    before = db.get(claim.job_id)
    monkeypatch.setattr(versions, "EXPLANATION_POLICY_VERSION", "neutral-summary-v2-test")
    forbidden = Mock(side_effect=AssertionError("Historical DONE must not re-run any postprocessing"))
    for name in ("analyze_reviews", "naturalize_reasons_batch", "neutral_reasons"):
        monkeypatch.setattr(data_analysis, name, forbidden)
    response = http.post("/api/v1/data/analyze/stream", json=body(),
                         headers={"X-Request-ID": "old-retry", "Idempotency-Key": key})
    assert response.status_code == 200
    events = decode(response.text)
    assert [name for name, _ in events] == ["meta", "result", "result", "done"]
    assert next(row for name, row in events if name == "result")["reasons"] == []
    assert "explanation_policy_version" not in events[0][1]
    assert db.get(claim.job_id) == before
    forbidden.assert_not_called()


def test_sqlite_existing_schema_upgrade_preserves_data_and_null(tmp_path):
    path = str(tmp_path / "legacy.db")
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE ai_analysis_jobs(job_id TEXT PRIMARY KEY,status TEXT NOT NULL,
            request_json TEXT NOT NULL,result_json TEXT,error_code TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
        db.execute("INSERT INTO ai_analysis_jobs(job_id,status,request_json,result_json) VALUES ('old','DONE',?,?)",
                   (json.dumps({"content": "기존 입력"}), json.dumps({"reasons": [], "rti": -1, "level": None})))
    store = SQLiteJobStore(path)
    store.initialize()
    before = store.get("old")
    assert before["explanation_policy_version"] is None
    assert before["result"] == {"reasons": [], "rti": -1, "level": None}
    new = store.create({})
    store.complete(new, {"same": True}, explanation_policy_version="neutral-summary-v1")
    store.initialize()
    assert store.get("old") == before
    assert store.get(new)["explanation_policy_version"] == "neutral-summary-v1"


def test_sqlite_result_and_version_roll_back_together(tmp_path):
    store = SQLiteJobStore(str(tmp_path / "atomic.db"))
    store.initialize()
    job = store.create({})
    with closing(store._connect()) as db, db:
        db.execute("""CREATE TRIGGER reject_result BEFORE UPDATE ON ai_analysis_jobs
            WHEN NEW.explanation_policy_version IS NOT NULL BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        store.complete(job, {"result": True}, explanation_policy_version="neutral-summary-v1")
    saved = store.get(job)
    assert saved["status"] == "RUNNING" and saved["result"] is None
    assert saved["explanation_policy_version"] is None


def test_mysql_result_and_version_rollback_on_rejected_update(isolated_schema):
    import pymysql
    from uuid import uuid4
    store = isolated_schema
    store.initialize()
    job = store.create({"atomic": True})
    # This whole UUID-owned schema is disposable; the trigger never touches another database.
    trigger = "reject_explanation_" + uuid4().hex
    with store._connection() as connection, connection.cursor() as cursor:
        cursor.execute(f"""CREATE TRIGGER `{trigger}` BEFORE UPDATE ON ai_analysis_jobs
            FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='synthetic rejected update'""")
    with pytest.raises(pymysql.MySQLError):
        store.complete(job, {"result": True}, explanation_policy_version="neutral-summary-v1")
    saved = store.get(job)
    assert saved["status"] == "RUNNING" and saved["result"] is None
    assert saved["explanation_policy_version"] is None
