"""MySQL 설정·커밋·롤백과 선택 실행하는 실제 DB 저장 계약을 검증한다."""
import os
import json
from unittest.mock import MagicMock

import pymysql
import pytest
from fastapi.testclient import TestClient

from app.factory import create_app
from app.repositories.mysql_jobs import MySQLJobStore

pytestmark = pytest.mark.usefixtures("model_free_prediction")


def test_requires_mysql_credentials(monkeypatch):
    monkeypatch.delenv("DB_PASSWORD", raising=False)
    with pytest.raises(ValueError, match="SQLite fallback is disabled"):
        MySQLJobStore.from_env()


def test_mysql_env(monkeypatch):
    for key, value in dict(DB_HOST="db", DB_PORT="3306", DB_USER="ai",
                           DB_PASSWORD="test-only", DB_NAME="results").items():
        monkeypatch.setenv(key, value)
    assert MySQLJobStore.from_env()._settings == dict(
        host="db", port=3306, user="ai", password="test-only", database="results")


@pytest.fixture
def fake_db(monkeypatch):
    db = MagicMock()
    connect = MagicMock(return_value=db)
    monkeypatch.setattr(pymysql, "connect", connect)
    store = MySQLJobStore(host="db", port=3306, user="ai",
                          password="test-only", database="results")
    return store, db, connect


def test_create_commits_parameterized_unicode(fake_db):
    store, db, connect = fake_db
    job_id = store.create({"content": "한글 리뷰 😃 ' OR 1=1"})
    sql, args = db.cursor.return_value.__enter__.return_value.execute.call_args.args
    assert "%s" in sql and "한글" not in sql
    assert args[0] == job_id and "한글" in args[1]
    assert connect.call_args.kwargs["autocommit"] is False
    assert connect.call_args.kwargs["charset"] == "utf8mb4"
    db.commit.assert_called_once()
    db.rollback.assert_not_called()
    db.close.assert_called_once()


def test_complete_preserves_numeric_unavailable_and_null_level(fake_db):
    store, db, _ = fake_db
    response = {"platform": "mall", "product_id": "0007", "review_count": 1, "results": [{
        "review_id": "00:01", "rti": -1, "level": None, "text_score": -1,
        "behavior_score": -1, "network_score": -1, "reasons": [],
    }]}
    store.complete("job", response)
    sql, params = db.cursor.return_value.__enter__.return_value.execute.call_args.args
    assert "result_json=%s" in sql
    assert json.loads(params[0]) == response
    assert params[1] == "job"
    db.commit.assert_called_once()


@pytest.mark.parametrize("failure", ["execute", "commit"])
def test_failure_rolls_back_and_closes(fake_db, failure):
    store, db, _ = fake_db
    target = db.commit if failure == "commit" else db.cursor.return_value.__enter__.return_value.execute
    target.side_effect = RuntimeError("storage failed")
    with pytest.raises(RuntimeError, match="storage failed"):
        store.complete("job", {"rti": 88})
    db.rollback.assert_called_once()
    db.close.assert_called_once()


def test_default_app_uses_mysql_and_initializes(fake_db, monkeypatch):
    store, db, _ = fake_db
    monkeypatch.setenv("ENABLE_EXPERIMENTAL_COLLECTION", "0")
    monkeypatch.setattr(MySQLJobStore, "from_env", classmethod(lambda cls: store))
    with TestClient(create_app()) as client:
        assert client.app.state.job_store is store
        assert client.get("/health").status_code == 200
    sql = "\n".join(call.args[0] for call in db.cursor.return_value.__enter__.return_value.execute.call_args_list)
    assert "CREATE TABLE IF NOT EXISTS ai_analysis_jobs" in sql
    assert "InnoDB" in sql


def test_mysql_unavailable_stops_startup(fake_db, monkeypatch):
    store, _, connect = fake_db
    connect.side_effect = pymysql.OperationalError(2003, "unavailable")
    monkeypatch.setattr(MySQLJobStore, "from_env", classmethod(lambda cls: store))
    with pytest.raises(pymysql.OperationalError):
        with TestClient(create_app()):
            pass


@pytest.mark.skipif(os.getenv("RUN_MYSQL_TESTS") != "1",
                    reason="Requires an explicitly configured disposable MySQL database")
def test_live_mysql_lifecycle_and_http(monkeypatch):
    # RUN_MYSQL_TESTS=1은 전용 검증 DB에서만 사용한다. 생성한 job만 종료 시 정리한다.
    store = MySQLJobStore.from_env()
    store.initialize()
    ids = []
    monkeypatch.setenv("ENABLE_EXPERIMENTAL_COLLECTION", "0")
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    try:
        job_id = store.create({"content": "한글 😃"})
        ids.append(job_id)
        assert store.get(job_id)["status"] == "RUNNING"
        store.save_input(job_id, {"content": "수정 😃"})
        store.complete(job_id, {"rti": 88, "nullable": None})
        store.fail(job_id, "TOO_LATE")
        saved = MySQLJobStore.from_env().get(job_id)
        assert saved["status"] == "DONE" and saved["error_code"] is None
        assert saved["request"] == {"content": "수정 😃"}
        assert saved["result"] == {"rti": 88, "nullable": None}
        failed = store.create({})
        ids.append(failed)
        store.fail(failed, "TEST_FAILURE")
        assert store.get(failed)["status"] == "FAILED"
        assert store.get("missing") is None
        with TestClient(create_app(job_store=store)) as client:
            response = client.post("/api/v1/data/analyze", json={"platform": "test", "product_id": "mysql-check", "reviews": [{
                "review_id": "1001", "content": "배송 빠르고 제품도 좋아요",
            }]})
            assert response.status_code == 200
            ids.append(response.headers["X-Analysis-Job-ID"])
            assert response.json()["results"][0]["rti"] == 72
            assert response.json()["results"][0]["behavior_score"] == -1
            assert response.json()["results"][0]["network_score"] == -1
            assert store.get(ids[-1])["result"] == response.json()
            monkeypatch.setattr("app.services.analysis.predict_text_score", lambda content: {"text_score": -1})
            response = client.post("/api/v1/data/analyze", json={"platform": "test", "product_id": "mysql-missing", "reviews": [{
                "review_id": "00:01", "content": "누락 신호 검증 😃",
            }]})
            assert response.status_code == 200
            ids.append(response.headers["X-Analysis-Job-ID"])
            result = response.json()["results"][0]
            assert result["rti"] == result["text_score"] == result["behavior_score"] == result["network_score"] == -1
            assert result["level"] is None
            assert store.get(ids[-1])["result"] == response.json()
    finally:
        for job_id in ids:
            store._execute("DELETE FROM ai_analysis_jobs WHERE job_id=%s", (job_id,))
