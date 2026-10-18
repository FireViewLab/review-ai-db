"""검증된 AI 런타임과 운영 API·SSE·저장·health 경계를 함께 검증한다."""
import json
from dataclasses import replace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app.analyzers import p_text
from app.analyzers.behavior import BehaviorInput, analyze_behavior
from app.analyzers.network import NetworkReview, analyze_network_batch
from app.factory import create_app
from app.integrations.crawler_stream import iter_sse_frames
from app.repositories.analysis_jobs import SQLiteJobStore
from app.services.analysis import analyze_reviews
from tests.test_persisted_stream import FakeCrawler, review
from app.contracts.stream import DoneEvent


@pytest.fixture
def runtime_app(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setenv("INTERNAL_TOKEN", "integration-test-only")
    monkeypatch.setenv("REQUIRE_INTERNAL_TOKEN", "1")
    monkeypatch.setenv("ENABLE_EXPERIMENTAL_COLLECTION", "1")
    monkeypatch.setenv("DATA_SERVER_BASE_URL", "https://data.test")
    monkeypatch.delenv("DATA_INTERNAL_TOKEN", raising=False)
    store = SQLiteJobStore(str(tmp_path / "integrated.db"))
    app = create_app(job_store=store)
    with TestClient(app) as http:
        yield http, store


@pytest.mark.parametrize("text,behavior,network", [
    (-1, 70, 80), (90, -1, 80), (90, 70, -1), (-1, -1, -1),
])
def test_missing_signal_combinations_across_service_api_and_storage(
    runtime_app, monkeypatch, text, behavior, network,
):
    """신호 의존성만 제어하고 실제 RTI·응답 mapper·API·저장 경로를 실행한다."""
    monkeypatch.setattr("app.services.analysis.predict_text_scores",
                        lambda contents: [{"text_score": text} for _ in contents])
    behavior_result = analyze_behavior(BehaviorInput(verified_purchase=False))
    monkeypatch.setattr("app.services.analysis.analyze_behavior", lambda data: replace(
        behavior_result, available=behavior != -1,
        p_behavior=None if behavior == -1 else behavior,
        reasons=(), unavailable_reasons=(),
    ))
    unavailable_network = analyze_network_batch([NetworkReview("r", "p", "sample")])[0]
    monkeypatch.setattr("app.services.analysis.analyze_network_batch", lambda rows: tuple(
        replace(unavailable_network, available=network != -1,
                p_network=None if network == -1 else network, unavailable_reason=None)
        for row in rows
    ))
    body = {"platform": "mall", "product_id": "0007", "reviews": [{
        "review_id": "00:01", "content": "배송 빠르고 제품도 좋아요",
    }]}
    direct = analyze_reviews(**body)
    http, store = runtime_app
    response = http.post("/api/v1/data/analyze", json=body,
                         headers={"X-Internal-Token": "integration-test-only"})
    assert response.status_code == 200
    assert response.json() == direct
    saved = store.get(response.headers["X-Analysis-Job-ID"])
    assert saved["status"] == "DONE" and saved["result"] == direct
    result = direct["results"][0]
    assert [result[key] for key in ("text_score", "behavior_score", "network_score")] == [
        text, behavior, network,
    ]
    available = [(score, weight) for score, weight in zip(
        (text, behavior, network), (.5, .3, .2), strict=True,
    ) if score != -1]
    expected = round(sum(score * weight for score, weight in available) /
                     sum(weight for _, weight in available), 1) if available else -1
    assert result["rti"] == expected
    assert result["level"] == (None if expected == -1 else
                               "safe" if expected >= 70 else "warn" if expected >= 40 else "danger")


@pytest.mark.parametrize("text_score", [87, -1])
def test_api_and_sse_publish_identical_flat_contract(runtime_app, monkeypatch, text_score):
    monkeypatch.setattr("app.services.analysis.predict_text_scores",
                        lambda contents: [{"text_score": text_score} for _ in contents])
    http, store = runtime_app
    event = review("00:01").model_copy(update={"product_id": "0007"})
    http.app.state.crawler_stream = FakeCrawler([event, DoneEvent(job_id="up", collected=1)])
    body = {"platform": event.platform, "product_id": event.product_id, "reviews": [{
        "review_id": event.review_id, "content": event.content,
        "rating": None, "written_at": None,
    }]}
    headers = {"X-Internal-Token": "integration-test-only"}
    response = http.post("/api/v1/data/analyze", json=body, headers=headers)
    stream = http.post("/experimental/analysis/collect/stream",
                       json={"platform": event.platform, "product_id": event.product_id}, headers=headers)
    assert response.status_code == stream.status_code == 200
    frames = list(iter_sse_frames(stream.text.splitlines()))
    assert frames[-1].event == "result"
    streamed = json.loads(frames[-1].data)
    assert response.json() == streamed
    for reply in (response, stream):
        assert store.get(reply.headers["X-Analysis-Job-ID"])["result"] == streamed
    assert set(streamed) == {"platform", "product_id", "review_count", "results"}
    result = streamed["results"][0]
    assert result["review_id"] == "00:01"
    assert set(result) == {"review_id", "rti", "level", "text_score", "behavior_score", "network_score", "reasons"}
    assert all(not isinstance(value, dict) for value in result.values())
    assert result["text_score"] == text_score
    assert result["behavior_score"] == result["network_score"] == -1
    assert result["rti"] == text_score
    assert (result["level"] is None) == (text_score == -1)
    assert all(reason.startswith(("TEXT_", "BEHAVIOR_", "NETWORK_")) for reason in result["reasons"])


def test_missing_model_does_not_block_startup_or_health(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setenv("ENABLE_EXPERIMENTAL_COLLECTION", "0")
    monkeypatch.setenv("REQUIRE_INTERNAL_TOKEN", "0")
    monkeypatch.delenv("INTERNAL_TOKEN", raising=False)
    path = str(tmp_path / "model-does-not-exist")
    monkeypatch.setenv("PTEXT_MODEL_PATH", path)
    loader = Mock(side_effect=FileNotFoundError("model-does-not-exist"))
    monkeypatch.setattr(p_text, "_predictor", loader)
    with TestClient(create_app(job_store=SQLiteJobStore(str(tmp_path / "missing.db")))) as http:
        loader.assert_not_called()
        assert http.get("/health").json() == {"status": "ok"}
        loader.assert_not_called()
        response = http.post("/api/v1/data/analyze", json={
            "platform": "mall", "product_id": "p", "reviews": [{"review_id": "r", "content": "테스트 리뷰"}],
        })
        assert response.status_code == 200
        loader.assert_called_once_with(path)
        result = response.json()["results"][0]
        assert [result[key] for key in ("text_score", "behavior_score", "network_score", "rti")] == [-1] * 4
        assert result["level"] is None
        assert http.get("/health").status_code == 200
