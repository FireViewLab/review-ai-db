"""V2 SSE contract, version isolation, persistence, and fallback; no external Groq."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from time import sleep
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from app.analyzers.p_text import unavailable_result
from app.contracts.data_ai_v2 import DataAnalyzeRequestV2, v2_versions
from app.factory import create_app
from app.integrations.groq_v2_summary import V2GroqSummary, verified_paragraph
from app.repositories.analysis_jobs import SQLiteJobStore
from app.repositories.v2_file_jobs import FileV2JobStore, V2Job
from app.services import analysis, data_analysis_v2
from app.services import data_analysis_stream
from app.v2_server import create_v2_app


V1 = "/api/v1/data/analyze/stream"
V2 = "/api/v2/data/analyze/stream"
HEADERS = {"X-Request-ID": "data-request", "Idempotency-Key": "same-key", "X-Internal-Token": "synthetic-test"}
BODY = {"platform": "kurly", "product_id": "001", "reviews": [{"review_id": "001", "content": "좋아요!"}]}


def events(response):
    return [(lines[0].removeprefix("event: "), json.loads(lines[1].removeprefix("data: ")))
            for block in response.text.strip().split("\n\n") if (lines := block.splitlines())]


def prediction(score):
    return {"text_score": score, "predicted_label": "NORMAL", "suspicious_probability": 0.2}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("ENABLE_DATA_AI_V2", "1")
    monkeypatch.setenv("REQUIRE_INTERNAL_TOKEN", "1")
    monkeypatch.setenv("INTERNAL_TOKEN", "synthetic-test")
    monkeypatch.setenv("ENABLE_GROQ_REASON_NATURALIZATION", "0")
    monkeypatch.setenv("ENABLE_V2_GROQ_SUMMARY", "0")


@pytest.fixture
def stores(tmp_path, env):
    return SQLiteJobStore(str(tmp_path / "v1.sqlite")), FileV2JobStore(tmp_path / "v2")


def test_v2_disabled_by_default(stores, monkeypatch):
    monkeypatch.setenv("ENABLE_DATA_AI_V2", "0")
    with TestClient(create_app(job_store=stores[0], v2_job_store=stores[1])) as client:
        assert V2 not in client.get("/openapi.json").json()["paths"]
        assert client.post(V2, json=BODY, headers=HEADERS).status_code == 404


@pytest.mark.parametrize("field,value", [("image_count", -1), ("image_count", 1.5), ("image_count", True),
                                          ("image_count", "0"), ("rating", 5.1), ("rating", -1),
                                          ("rating", "5"), ("foo", 0), ("content", "  ")])
def test_v2_requires_auth_and_strict_input(stores, field, value):
    with TestClient(create_app(job_store=stores[0], v2_job_store=stores[1])) as client:
        assert client.post(V2, headers={"X-Request-ID": "a", "Idempotency-Key": "b"}, json=BODY).status_code in (401, 403)
        altered = {**BODY, "reviews": [{**BODY["reviews"][0], field: value}]}
        assert client.post(V2, headers=HEADERS, json=altered).status_code == 422


def test_v2_missing_duplicate_and_limits():
    rows = [{"review_id": str(i), "content": "검증용 리뷰"} for i in range(501)]
    assert len(DataAnalyzeRequestV2.model_validate({**BODY, "reviews": rows[:500]}).reviews) == 500
    for invalid in ([], rows, rows[:1] * 2):
        with pytest.raises(ValueError):
            DataAnalyzeRequestV2.model_validate({**BODY, "reviews": invalid})


def test_undefined_signals_distinguished_from_runtime_failure():
    response = data_analysis_v2.evaluate_v2(DataAnalyzeRequestV2.model_validate(BODY), predict=lambda _: [unavailable_result()])
    row = response.results[0]
    assert (row.text_score, row.behavior_score, row.network_score, row.policy_index_v3, row.evidence_level_v3) == (-1, -1, -1, -1, None)
    assert row.reason_source == "rules" and row.analysis_summary_ko is None


def assert_parallel_streams(stores, monkeypatch):
    v1, v2 = stores
    gate = Barrier(2, timeout=4)
    counts = {"v1": 0, "v2": 0, "groq": 0}
    headers = {**HEADERS, "Idempotency-Key": uuid4().hex}

    def predictor(version):
        def mock(contents):
            counts[version] += 1
            gate.wait()
            return [prediction(85 if version == "v1" else 53) for _ in contents]
        return mock

    def groq(request):
        counts["groq"] += 1
        body = json.loads(request.content)
        assert body["model"] == "openai/gpt-oss-20b" and body["temperature"] == 0.3
        assert all(secret not in request.content.decode() for secret in ("좋아요", "review_id", "text_score"))
        items = json.loads(body["messages"][1]["content"])["items"]
        out = [{"item_id": obj["item_id"], "analysis_summary_ko": " 또한 ".join(obj["facts_ko"])} for obj in items]
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"items": out})}}]})

    monkeypatch.setattr(analysis, "predict_text_scores", predictor("v1"))
    monkeypatch.setattr(data_analysis_v2, "predict_v31b", predictor("v2"))
    richer = {**BODY, "reviews": [{**BODY["reviews"][0], "rating": 5, "image_count": 0}]}
    with TestClient(create_app(job_store=v1, v2_job_store=v2, v2_groq=V2GroqSummary(key="test-key", transport=httpx.MockTransport(groq)))) as c:
        with ThreadPoolExecutor(max_workers=2) as threads:
            one = threads.submit(c.post, V1, json=BODY, headers=headers)
            two = threads.submit(c.post, V2, json=richer, headers=headers)
            a, b = one.result(timeout=8), two.result(timeout=8)
        old, new = dict(events(a)), dict(events(b))
        assert old["meta"]["contract_version"] == "v0.5" and new["meta"]["contract_version"] == "v2"
        assert old["meta"]["model_version"] != new["meta"]["model_version"]
        assert old["result"]["rti"] == 85 and "policy_index_v3" not in old["result"]
        assert new["result"]["evidence_level_v3"] == "DANGER" and new["result"]["text_score"] == 53
        assert "rti" not in new["result"] and new["result"]["reason_source"] == "groq"
        assert "세 조건이 함께" in new["result"]["analysis_summary_ko"]
        assert new["done"]["result_count"] == 1
        old_id, new_id = (r.headers["X-Analysis-Job-ID"] for r in (a, b))
        assert old_id != new_id and v1.get(new_id) is None
        with v2.claim(headers["Idempotency-Key"], DataAnalyzeRequestV2.model_validate(richer).model_dump(mode="json"),
                      v2_versions()) as cached:
            assert not cached.claim.is_new and cached.claim.job_id == new_id
            assert cached.result["results"] == [{k: v for k, v in new["result"].items() if k != "request_id"}]
        for route, payload, expected in [(V1, BODY, old["result"]), (V2, richer, new["result"])]:
            replay = events(c.post(route, json=payload, headers=headers))
            assert [name for name, _ in replay] == ["meta", "result", "done"] and dict(replay)["result"] == expected
        assert counts == {"v1": 1, "v2": 1, "groq": 1}
        assert c.post(V2, json=BODY, headers=headers).json()["detail"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_v1_sqlite_and_v2_files_parallel_versions_and_replay(stores, monkeypatch):
    assert_parallel_streams(stores, monkeypatch)


def test_standalone_v2_replay_after_restart_without_mysql(tmp_path, monkeypatch, env):
    def forbidden(*args, **kwargs):
        pytest.fail("A v2 replay must not initialize MySQL or rerun the model")

    monkeypatch.setenv("V2_JOB_DIR", str(tmp_path / "replay"))
    monkeypatch.delenv("DB_PASSWORD", raising=False)
    monkeypatch.setattr("app.repositories.mysql_jobs.MySQLJobStore.from_env", forbidden)
    monkeypatch.setattr(data_analysis_v2, "predict_v31b", lambda _: [prediction(53)])
    with TestClient(create_v2_app()) as client:
        assert V1 not in client.get("/openapi.json").json()["paths"]
        fresh = client.post(V2, json=BODY, headers=HEADERS)
        assert [name for name, _ in events(fresh)] == ["meta", "progress", "result", "done"]
    monkeypatch.setattr(data_analysis_v2, "predict_v31b", forbidden)
    with TestClient(create_v2_app()) as client:
        replay = client.post(V2, json=BODY, headers={**HEADERS, "X-Request-ID": "replay"})
        frames = events(replay)
        assert [name for name, _ in frames] == ["meta", "result", "done"]
        assert all(row["request_id"] == "replay" for _, row in frames)
        assert replay.headers["X-Analysis-Job-ID"] == fresh.headers["X-Analysis-Job-ID"]
        assert dict(frames)["result"] == {**dict(events(fresh))["result"], "request_id": "replay"}


def test_v2_model_failure_keeps_v1_healthy(stores, monkeypatch):
    def unavailable():
        raise ValueError("do not expose path")

    monkeypatch.setattr(data_analysis_v2, "load_candidate_predictor", unavailable)
    monkeypatch.setattr(analysis, "predict_text_scores", lambda _: [prediction(85)])
    with TestClient(create_app(job_store=stores[0], v2_job_store=stores[1])) as c:
        failed = c.post(V2, headers=HEADERS, json=BODY)
        assert list(dict(events(failed))) == ["meta", "progress", "error"]
        assert dict(events(failed))["error"]["code"] == "V2_MODEL_UNAVAILABLE" and "expose path" not in failed.text
        assert c.post(V2, headers=HEADERS, json=BODY).status_code == 409
        assert c.get("/health").status_code == 200 and "done" in dict(events(c.post(V1, json=BODY, headers=HEADERS)))


def test_v2_heartbeat_and_no_result_on_commit_failure(stores, monkeypatch):
    def predict(contents):
        sleep(.08)
        return [prediction(53) for _ in contents]

    def no_commit(*args, **kwargs):
        raise RuntimeError("isolated write failure")

    monkeypatch.setattr(data_analysis_v2, "predict_v31b", predict)
    monkeypatch.setattr(data_analysis_stream, "HEARTBEAT_SECONDS", .01)
    monkeypatch.setattr(V2Job, "complete", no_commit)
    with TestClient(create_app(job_store=stores[0], v2_job_store=stores[1])) as client:
        response = client.post(V2, headers=HEADERS, json=BODY)
        names = [name for name, _ in events(response)]
        assert names[:2] == ["meta", "progress"] and names[-1] == "error" and "heartbeat" in names
        assert "done" not in names and "result" not in names
        snapshot = json.loads(next(stores[1].root.glob("*.json")).read_text(encoding="utf-8"))
        assert snapshot["status"] == "RUNNING" and "result" not in snapshot


@pytest.mark.parametrize("mutation", ["rating", "absence", "add", "omit", "reorder", "invent"])
def test_only_structural_connectives_are_accepted(mutation):
    base = "사진 개수와 별점은 확인되지 않았습니다. 사진 조건은 판정에 쓰지 않았습니다."
    unsafe = {"rating": base.replace("별점은 확인되지 않았습니다", "별점은 5점입니다"), "absence": base.replace("사진 개수", "사진 0장"),
              "add": base + " 실제 사용을 확인했습니다.", "omit": base.split(". ")[0] + ".",
              "reorder": ". ".join(reversed(base.removesuffix(".").split(". "))) + ".", "invent": base + " 감점 영향이 가장 컸습니다."}
    assert verified_paragraph(base, unsafe[mutation]) is None
    assert verified_paragraph(base, base.replace(". ", ". 또한 ")) is not None


@pytest.mark.parametrize("failure", ["429", "timeout", "json", "length"])
def test_groq_failure_does_not_change_score_or_retry(failure):
    calls = []

    def fake(request):
        calls.append(request)
        if failure == "429":
            return httpx.Response(429, headers={"retry-after": "90"})
        if failure == "timeout":
            raise httpx.ReadTimeout("test")
        if failure == "length":
            return httpx.Response(200, json={"choices": [{"finish_reason": "length"}]})
        return httpx.Response(200, content=b"invalid JSON")

    client = V2GroqSummary(key="test-key", transport=httpx.MockTransport(fake))
    payload = DataAnalyzeRequestV2.model_validate(BODY)
    predict = lambda _: [prediction(53)]
    assert data_analysis_v2.evaluate_v2(payload, predict=predict, groq=client) == data_analysis_v2.evaluate_v2(payload, predict=predict)
    assert client.enrich(["검증용 문장입니다."]).status == "cooldown"
    assert len(calls) == 1
