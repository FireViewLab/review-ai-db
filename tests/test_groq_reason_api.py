"""이유 문장만 바뀌고 분석·인증·MySQL 저장·SSE 결과는 일치함을 검증한다."""
import json
import os
from copy import deepcopy
from unittest.mock import MagicMock, patch

import httpx
import pytest
import pymysql
from fastapi.testclient import TestClient

from app.contracts.data_ai_v05 import DataAnalyzeRequestV05
from app.contracts.stream import DoneEvent, ReviewEvent
from app.factory import create_app
from app.integrations import groq_reason_naturalizer as naturalizer
from app.integrations.crawler_stream import iter_sse_frames
from app.repositories.analysis_jobs import SQLiteJobStore
from app.repositories.mysql_jobs import MySQLJobStore
from app.services import data_analysis
from app.services.analysis import analyze_reviews
from tests.test_groq_reason_naturalizer import MESSAGE, input_items, completion, valid_output, PRIMARY, SECONDARY
from tests.test_persisted_stream import FakeCrawler


TOKEN = "synthetic-api-integration-token"


def body():
    return {"platform": "mall", "product_id": "0007", "reviews": [
        {"review_id": "00:01", "content": "좋아요"},
        {"review_id": "00:02", "content": "좋아요"},
    ]}


def baseline(payload):
    return analyze_reviews(platform=payload["platform"], product_id=payload["product_id"], reviews=payload["reviews"])


def assert_only_reasons_changed(before, after):
    assert set(before) == set(after) == {"platform", "product_id", "review_count", "results"}
    assert {key: value for key, value in before.items() if key != "results"} == {
        key: value for key, value in after.items() if key != "results"
    }
    for old, new in zip(before["results"], after["results"], strict=True):
        assert set(old) == set(new)
        assert {key: value for key, value in old.items() if key != "reasons"} == {
            key: value for key, value in new.items() if key != "reasons"
        }
        assert len(old["reasons"]) == len(new["reasons"])


@pytest.fixture
def configured(monkeypatch, model_free_prediction):
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setenv("ENABLE_GROQ_REASON_NATURALIZATION", "1")
    monkeypatch.setenv("GROQ_API_KEY_PRIMARY", PRIMARY)
    monkeypatch.setenv("GROQ_API_KEY_SECONDARY", SECONDARY)
    monkeypatch.setenv("GROQ_MODEL", "synthetic-test-model")
    monkeypatch.setenv("GROQ_TIMEOUT_SECONDS", "5")
    monkeypatch.setenv("ENABLE_EXPERIMENTAL_COLLECTION", "1")
    monkeypatch.setenv("DATA_SERVER_BASE_URL", "https://data.test")
    monkeypatch.setenv("DATA_INTERNAL_TOKEN", "synthetic-crawler-token")
    monkeypatch.setenv("INTERNAL_TOKEN", TOKEN)
    monkeypatch.setenv("REQUIRE_INTERNAL_TOKEN", "1")


@pytest.fixture
def api(configured, tmp_path):
    store = SQLiteJobStore(str(tmp_path / "groq.db"))
    with TestClient(create_app(job_store=store)) as client:
        yield client, store


def install_transport(monkeypatch, handler):
    """Groq 통신만 주입하며 실제 분석·검증·저장은 그대로 실행한다."""
    transport = httpx.MockTransport(handler)

    def enrich(*, contents, reasons):
        return naturalizer.naturalize_reasons_batch(contents=contents, reasons=reasons, transport=transport)

    monkeypatch.setattr(data_analysis, "naturalize_reasons_batch", enrich)


def test_success_api_preserves_scores_ids_and_saves_naturalized_reasons(api, monkeypatch):
    client, store = api
    payload = body()
    before = baseline(payload)
    calls = []

    def handler(request):
        items = input_items(request)
        calls.append(items)
        assert [item["content"] for item in items] == [row["content"] for row in payload["reviews"]]
        assert [item["reasons"] for item in items] == [row["reasons"] for row in before["results"]]
        return completion(valid_output(request))

    install_transport(monkeypatch, handler)
    response = client.post("/api/v1/data/analyze", json=payload, headers={"X-Internal-Token": TOKEN})
    assert response.status_code == 200
    result = response.json()
    assert_only_reasons_changed(before, result)
    assert [row["review_id"] for row in result["results"]] == ["00:01", "00:02"]
    assert all(row["reasons"] == [MESSAGE] * len(original["reasons"])
               for original, row in zip(before["results"], result["results"], strict=True))
    saved = store.get(response.headers["X-Analysis-Job-ID"])
    assert saved["status"] == "DONE" and saved["result"] == result
    assert saved["request"] == DataAnalyzeRequestV05(**payload).model_dump(mode="json")
    assert len(calls) == 1


@pytest.mark.parametrize("failure", ["http", "timeout", "connection", "malformed"])
def test_provider_failure_still_returns_200_original_reasons_and_stores_same(api, monkeypatch, failure):
    client, store = api
    payload = body()
    expected = baseline(payload)
    calls = []

    def handler(request):
        calls.append(request)
        if failure == "http": return httpx.Response(503)
        if failure == "malformed": return completion("not JSON")
        error = httpx.ReadTimeout if failure == "timeout" else httpx.ConnectError
        raise error("synthetic provider failure", request=request)

    install_transport(monkeypatch, handler)
    response = client.post("/api/v1/data/analyze", json=payload, headers={"X-Internal-Token": TOKEN})
    assert response.status_code == 200 and response.json() == expected
    assert store.get(response.headers["X-Analysis-Job-ID"])["result"] == expected
    assert len(calls) == (1 if failure == "malformed" else 2)


def test_disabled_naturalization_preserves_original_and_never_requests(api, monkeypatch):
    client, store = api
    monkeypatch.setenv("ENABLE_GROQ_REASON_NATURALIZATION", "0")
    install_transport(monkeypatch, lambda request: pytest.fail("Disabled feature made provider request"))
    response = client.post("/api/v1/data/analyze", json=body(), headers={"X-Internal-Token": TOKEN})
    assert response.status_code == 200 and response.json() == baseline(body())
    assert store.get(response.headers["X-Analysis-Job-ID"])["result"] == response.json()


def test_auth_and_health_do_not_call_groq_or_model(api, monkeypatch):
    client, store = api
    install_transport(monkeypatch, lambda request: pytest.fail("Auth/health made provider request"))
    with patch("app.services.analysis.predict_text_score") as model, patch.object(store, "create") as create:
        assert client.get("/health").status_code == 200
        assert client.post("/api/v1/data/analyze", json=body()).status_code == 401
        assert client.post("/experimental/analysis/collect/stream", json={"platform": "mall", "product_id": "0007"}).status_code == 401
    model.assert_not_called()
    create.assert_not_called()


def test_sse_and_api_same_naturalization_and_saved_json(api, monkeypatch):
    client, store = api
    install_transport(monkeypatch, lambda request: completion(valid_output(request)))
    payload = body()
    events = [ReviewEvent(platform="mall", product_id="0007", **row) for row in payload["reviews"]]
    client.app.state.crawler_stream = FakeCrawler([*events, DoneEvent(job_id="synthetic-upstream", collected=2)])
    headers = {"X-Internal-Token": TOKEN}
    response = client.post("/api/v1/data/analyze", json=payload, headers=headers)
    stream = client.post("/experimental/analysis/collect/stream",
                         json={"platform": "mall", "product_id": "0007", "limit": 2}, headers=headers)
    assert response.status_code == stream.status_code == 200
    frames = list(iter_sse_frames(stream.text.splitlines()))
    assert frames[-1].event == "result"
    result = json.loads(frames[-1].data)
    assert result == response.json()
    for reply in (response, stream):
        assert store.get(reply.headers["X-Analysis-Job-ID"])["result"] == result
    lookup = client.get("/experimental/analysis/jobs/" + stream.headers["X-Analysis-Job-ID"], headers=headers)
    assert lookup.status_code == 200 and lookup.json()["result"] == result


def test_successful_enrichment_does_not_hide_storage_failure(api, monkeypatch):
    client, store = api
    install_transport(monkeypatch, lambda request: completion(valid_output(request)))
    with patch.object(store, "complete", side_effect=OSError("synthetic storage failure")):
        response = client.post("/api/v1/data/analyze", json=body(), headers={"X-Internal-Token": TOKEN})
    assert response.status_code == 503 and "results" not in response.json()


@pytest.mark.parametrize("failure", ["exception", "missing_rows", "invalid_schema", "non_list"])
def test_postprocessor_exception_or_invalid_schema_falls_back_not_endpoint_error(api, monkeypatch, failure):
    client, store = api

    def broken(*, contents, reasons):
        if failure == "exception": raise RuntimeError("synthetic postprocessing exception")
        if failure == "missing_rows": return []
        if failure == "invalid_schema": return [{"rti": 0} for _ in reasons]
        return None

    monkeypatch.setattr(data_analysis, "naturalize_reasons_batch", broken)
    expected = baseline(body())
    response = client.post("/api/v1/data/analyze", json=body(), headers={"X-Internal-Token": TOKEN})
    assert response.status_code == 200 and response.json() == expected
    assert store.get(response.headers["X-Analysis-Job-ID"])["result"] == expected


def test_source_content_maps_by_review_id_if_analyzer_result_order_changes(api, monkeypatch):
    client, store = api
    payload = body()
    payload["reviews"][0]["content"] = "첫 번째 리뷰"
    payload["reviews"][1]["content"] = "두 번째 리뷰"
    expected = deepcopy(baseline(payload))
    expected["results"].reverse()
    monkeypatch.setattr(data_analysis, "analyze_reviews", lambda **kwargs: deepcopy(expected))

    def handler(request):
        assert [item["content"] for item in input_items(request)] == ["두 번째 리뷰", "첫 번째 리뷰"]
        return completion(valid_output(request))

    install_transport(monkeypatch, handler)
    response = client.post("/api/v1/data/analyze", json=payload, headers={"X-Internal-Token": TOKEN})
    assert response.status_code == 200
    assert [row["review_id"] for row in response.json()["results"]] == ["00:02", "00:01"]
    assert_only_reasons_changed(expected, response.json())
    assert store.get(response.headers["X-Analysis-Job-ID"])["result"] == response.json()


def test_mysql_json_receives_exact_naturalized_response(configured, monkeypatch):
    db = MagicMock()
    monkeypatch.setattr(pymysql, "connect", lambda **kwargs: db)
    store = MySQLJobStore(host="validation-only", port=3306, user="synthetic-user",
                          password="synthetic-password", database="validation-only")
    install_transport(monkeypatch, lambda request: completion(valid_output(request)))
    before = baseline(body())
    with TestClient(create_app(job_store=store)) as client:
        response = client.post("/api/v1/data/analyze", json=body(), headers={"X-Internal-Token": TOKEN})
    assert response.status_code == 200
    calls = db.cursor.return_value.__enter__.return_value.execute.call_args_list
    updates = [call.args for call in calls if "result_json=%s" in call.args[0]]
    assert len(updates) == 1
    sql, params = updates[0]
    assert "%s" in sql and MESSAGE not in sql
    assert json.loads(params[0]) == response.json()
    assert params[1] == response.headers["X-Analysis-Job-ID"]
    assert_only_reasons_changed(before, response.json())


@pytest.mark.skipif(os.getenv("RUN_MYSQL_TESTS") != "1", reason="Dedicated disposable MySQL required")
def test_live_mysql_naturalized_api_is_durable(configured, monkeypatch):
    store = MySQLJobStore.from_env()
    install_transport(monkeypatch, lambda request: completion(valid_output(request)))
    job_id = None
    try:
        with TestClient(create_app(job_store=store)) as client:
            response = client.post("/api/v1/data/analyze", json=body(), headers={"X-Internal-Token": TOKEN})
        assert response.status_code == 200
        job_id = response.headers["X-Analysis-Job-ID"]
        saved = store.get(job_id)
        assert saved["status"] == "DONE" and saved["result"] == response.json()
        assert all(reason == MESSAGE for row in saved["result"]["results"] for reason in row["reasons"])
    finally:
        if job_id is not None:
            store._execute("DELETE FROM ai_analysis_jobs WHERE job_id=%s", (job_id,))
