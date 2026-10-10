"""공식 POST→SSE의 인증·계약·저장 선행·replay·연결 수명을 검증한다."""

import asyncio
import json
import threading
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.contracts.data_ai_v05 import DataAnalyzeRequestV05
from app.core.analysis_versions import analysis_versions
from app.factory import create_app
from app.repositories.analysis_jobs import SQLiteJobStore
from app.services import data_analysis_stream as service
from tests.test_data_ai_v05 import payload

PATH = "/api/v1/data/analyze/stream"
HEADERS = {"X-Request-ID": "data-request-1", "Idempotency-Key": "test-job-1"}
pytestmark = pytest.mark.usefixtures("model_free_prediction")


def decode(text):
    return [(part.splitlines()[0][7:], json.loads(part.splitlines()[1][6:]))
            for part in text.strip().split("\n\n")]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setenv("ENABLE_EXPERIMENTAL_COLLECTION", "0")
    monkeypatch.setenv("ENABLE_GROQ_REASON_NATURALIZATION", "0")
    monkeypatch.setenv("REQUIRE_INTERNAL_TOKEN", "0")
    monkeypatch.delenv("INTERNAL_TOKEN", raising=False)
    store = SQLiteJobStore(str(tmp_path / "stream.db"))
    with TestClient(create_app(job_store=store)) as http:
        yield http, store


@pytest.mark.parametrize("duplicate", [True, False])
def test_matches_json_and_commits_before_first_result(client, monkeypatch, duplicate):
    http, store = client
    body = payload()
    body["reviews"].append({**body["reviews"][0], "review_id": "second"})
    if not duplicate:
        body["reviews"][0]["content"] = "이 크림을 한 달 사용하니 세안 후 당김이 줄고 촉촉해서 만족합니다."
        body["reviews"][1]["content"] = "배송 상자가 찌그러졌고 손잡이 나사가 빠져 반품했습니다."
    baseline = http.post("/api/v1/data/analyze", json=body).json()
    if not duplicate:
        assert all(r["network_score"] == -1 and r["rti"] == r["text_score"] == 72
                   for r in baseline["results"])
        assert all(not any(reason.startswith("NETWORK_") for reason in r["reasons"])
                   for r in baseline["results"])
    original_frame = service.frame
    job_ids = []

    def checked_frame(name, data):
        if name == "meta":
            job_ids.append(data["ai_job_id"])
        if name in {"result", "done"}:
            saved = SQLiteJobStore(store.path).get(job_ids[0])
            assert saved["status"] == "DONE"
            assert saved["result"] == baseline
        return original_frame(name, data)

    monkeypatch.setattr(service, "frame", checked_frame)
    response = http.post(PATH, json=body, headers=HEADERS)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-request-id"] == HEADERS["X-Request-ID"]
    events = decode(response.text)
    meta = events[0][1]
    assert events[0][0] == "meta"
    assert meta == {"request_id": "data-request-1", "ai_job_id": response.headers["x-analysis-job-id"],
                    "platform": "mall", "product_id": "0007", "review_count": 2, **analysis_versions()}
    results = [dict(data) for name, data in events if name == "result"]
    for result in results:
        assert result.pop("request_id") == "data-request-1"
    assert results == baseline["results"]
    assert events[1] == ("progress", {"request_id": "data-request-1", "stage": "analyzing", "processed": 0, "total": 2})
    assert events[-1] == ("done", {"request_id": "data-request-1", "ai_job_id": meta["ai_job_id"], "result_count": 2})


@pytest.mark.parametrize("header", ["X-Request-ID", "Idempotency-Key"])
@pytest.mark.parametrize("value", [None, "", "   ", "\t"])
def test_required_nonblank_headers(client, header, value):
    headers = dict(HEADERS)
    if value is None:
        headers.pop(header)
    else:
        headers[header] = value
    assert client[0].post(PATH, json=payload(), headers=headers).status_code == 422


def test_authentication_same_policy(client):
    http, _ = client
    http.app.state.internal_token = SecretStr("synthetic-test-token")
    for supplied in (None, "wrong"):
        headers = dict(HEADERS)
        if supplied:
            headers["X-Internal-Token"] = supplied
        assert http.post(PATH, json=payload(), headers=headers).status_code == 401
    response = http.post(PATH, json=payload(), headers={**HEADERS, "X-Internal-Token": "synthetic-test-token"})
    assert response.status_code == 200


def test_replay_uses_db_after_app_restart_and_never_analyzes(client, monkeypatch):
    http, store = client
    first = http.post(PATH, json=payload(), headers=HEADERS)
    analyzer = Mock(side_effect=AssertionError("Replay must not analyze"))
    monkeypatch.setattr(service, "evaluate_data_and_store", analyzer)
    monkeypatch.setattr("app.api.data_analysis_stream.analysis_versions", lambda: {"policy_version": "future"})
    with TestClient(create_app(job_store=SQLiteJobStore(store.path))) as restarted:
        replay = restarted.post(PATH, json=payload(), headers={**HEADERS, "X-Request-ID": "new-attempt"})
    assert replay.status_code == 200
    assert replay.headers["x-analysis-job-id"] == first.headers["x-analysis-job-id"]
    events = decode(replay.text)
    assert [name for name, _ in events] == ["meta", "result", "done"]
    assert all(data["request_id"] == "new-attempt" for _, data in events)
    assert events[0][1]["policy_version"] == "rti-v0.1"
    assert events[1][1]["behavior_score"] == -1
    analyzer.assert_not_called()


def test_in_progress_and_different_payload_conflicts(client):
    http, store = client
    body = payload()
    store.claim_idempotent(HEADERS["Idempotency-Key"], DataAnalyzeRequestV05.model_validate(body).model_dump(mode="json"), analysis_versions())
    same = http.post(PATH, json=body, headers=HEADERS)
    assert same.status_code == 409 and same.json()["detail"]["code"] == "IDEMPOTENCY_IN_PROGRESS"
    body["reviews"][0]["content"] = "changed"
    changed = http.post(PATH, json=body, headers=HEADERS)
    assert changed.status_code == 409 and changed.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_simultaneous_http_requests_start_analysis_once(client, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    http, _ = client
    entered, release = threading.Event(), threading.Event()
    original = service.evaluate_data_and_store

    def slow(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)

    analyzer = Mock(side_effect=slow)
    monkeypatch.setattr(service, "evaluate_data_and_store", analyzer)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(http.post, PATH, json=payload(), headers=HEADERS)
        try:
            assert entered.wait(5)
            second = http.post(PATH, json=payload(), headers=HEADERS)
            assert second.status_code == 409
            assert second.json()["detail"]["code"] == "IDEMPOTENCY_IN_PROGRESS"
        finally:
            release.set()
        assert decode(first.result(timeout=5).text)[-1][0] == "done"
    analyzer.assert_called_once()


def test_failed_key_and_corrupt_replay_never_reanalyze(client, monkeypatch):
    http, store = client
    data = DataAnalyzeRequestV05.model_validate(payload()).model_dump(mode="json")
    claim = store.claim_idempotent(HEADERS["Idempotency-Key"], data, analysis_versions())
    analyzer = Mock(side_effect=AssertionError("Must not analyze"))
    monkeypatch.setattr(service, "evaluate_data_and_store", analyzer)
    store.fail(claim.job_id, "FAILED")
    failed = http.post(PATH, json=payload(), headers=HEADERS)
    assert failed.status_code == 409
    assert failed.json()["detail"]["code"] == "IDEMPOTENCY_FAILED"
    store.complete(claim.job_id, {"broken": True})
    assert http.post(PATH, json=payload(), headers=HEADERS).status_code == 503
    analyzer.assert_not_called()


@pytest.mark.parametrize("failure", ["analyze", "complete", "read"])
def test_safe_stream_error_without_result_or_done(client, monkeypatch, failure):
    http, store = client
    target, name = (service, "evaluate_data_and_store") if failure == "analyze" else (store, "complete" if failure == "complete" else "get")
    monkeypatch.setattr(target, name, Mock(side_effect=RuntimeError("private-internal-diagnostic")))
    response = http.post(PATH, json=payload(), headers=HEADERS)
    events = decode(response.text)
    assert response.status_code == 200
    assert events[-1][0] == "error"
    assert events[-1][1]["code"] == "ANALYSIS_FAILED"
    assert events[-1][1]["request_id"] == "data-request-1"
    assert events[-1][1]["ai_job_id"] == response.headers["x-analysis-job-id"]
    assert not {"result", "done"} & {name for name, _ in events}
    assert "private-internal-diagnostic" not in response.text


def test_pre_stream_storage_failure(client, monkeypatch):
    monkeypatch.setattr(client[1], "claim_idempotent", Mock(side_effect=OSError("private")))
    response = client[0].post(PATH, json=payload(), headers=HEADERS)
    assert response.status_code == 503
    assert "private" not in response.text


@pytest.mark.parametrize("invalid", ["501", "empty", "duplicate", "extra", "review-extra"])
def test_request_validation(client, invalid):
    body = payload()
    if invalid == "501":
        body["reviews"] = [{**body["reviews"][0], "review_id": str(i)} for i in range(501)]
    elif invalid == "empty":
        body["reviews"] = []
    elif invalid == "duplicate":
        body["reviews"] *= 2
    elif invalid == "extra":
        body["request_id"] = "not-in-body"
    else:
        body["reviews"][0]["extra"] = "forbidden"
    assert client[0].post(PATH, json=body, headers=HEADERS).status_code == 422


def test_500_one_analysis_and_unavailable_empty_reasons(client, monkeypatch):
    body = payload()
    body["reviews"] = [{**body["reviews"][0], "review_id": str(i)} for i in range(500)]

    def evaluate(store, job_id, data):
        result = dict(platform=data.platform, product_id=data.product_id, review_count=500,
                      results=[dict(review_id=r.review_id, rti=-1, level=None, text_score=-1,
                                    behavior_score=-1, network_score=-1, reasons=[]) for r in data.reviews])
        store.complete(job_id, result)

    analyze = Mock(side_effect=evaluate)
    monkeypatch.setattr(service, "evaluate_data_and_store", analyze)
    response = client[0].post(PATH, json=body, headers=HEADERS)
    events = decode(response.text)
    results = [data for name, data in events if name == "result"]
    assert len(results) == 500
    assert [r["review_id"] for r in results] == [str(i) for i in range(500)]
    assert all(r["rti"] == -1 and r["level"] is None and r["reasons"] == [] for r in results)
    assert events[-1][1]["result_count"] == 500
    analyze.assert_called_once()


def test_heartbeat_during_thread_work_and_disconnect_persists(client, monkeypatch):
    _, store = client
    original = service.evaluate_data_and_store
    entered, release = threading.Event(), threading.Event()

    def slow(*args):
        entered.set()
        assert release.wait(3)
        return original(*args)

    monkeypatch.setattr(service, "evaluate_data_and_store", slow)
    monkeypatch.setattr(service, "HEARTBEAT_SECONDS", 0.01)

    async def scenario():
        data = DataAnalyzeRequestV05.model_validate(payload())
        claim = store.claim_idempotent("disconnect", data.model_dump(mode="json"), analysis_versions())
        tasks = set()
        task = service.start_analysis(tasks, store, claim.job_id, data)
        stream = service.analysis_events(request_id="disconnect-request", claim=claim, payload=data, task=task)
        assert decode((await anext(stream)).decode())[0][0] == "meta"
        assert decode((await anext(stream)).decode())[0][0] == "progress"
        for _ in range(100):
            assert decode((await anext(stream)).decode())[0][0] == "heartbeat"
            if entered.is_set():
                break
        assert entered.is_set() and not task.done()
        await stream.aclose()
        assert not task.cancelled() and task in tasks
        release.set()
        await task
        await asyncio.sleep(0)
        assert not tasks
        assert store.get(claim.job_id)["status"] == "DONE"

    asyncio.run(scenario())


def test_openapi_documents_stream_without_changing_json_schema(client):
    schema = client[0].get("/openapi.json").json()
    route = schema["paths"][PATH]["post"]
    assert set(route["responses"]["200"]["content"]) == {"text/event-stream"}
    assert route["security"] == schema["paths"]["/api/v1/data/analyze"]["post"]["security"]
    assert route["requestBody"] == schema["paths"]["/api/v1/data/analyze"]["post"]["requestBody"]
    assert {p["name"] for p in route["parameters"] if p["required"]} == {"x-request-id", "idempotency-key"}
