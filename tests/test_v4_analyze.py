"""P_text v4.0 단독 서비스: 인증, 입력 검증, 등급·지수 변환. 실제 모델 없이 확률 함수를 바꿔 끼운다."""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.analyzers.p_text_v4 import V4Policy, V4Predictor, grade, policy_index
from app.api.v4_analyze import router

POLICY = V4Policy("text-first-v1", 0.3, 0.8)
HEADERS = {"X-Internal-Token": "synthetic-test"}


def fake_probability(texts):
    # 본문에 숫자로 확률을 넣어 결과를 예측할 수 있게 한다.
    return [float(t) for t in texts]


def make_client(predictor=None):
    app = FastAPI()
    app.state.internal_token = SecretStr("synthetic-test")
    if predictor is not None:
        app.state.v4_predictor = predictor
    app.include_router(router)
    return TestClient(app)


@pytest.fixture
def client():
    return make_client(V4Predictor(fake_probability, POLICY, "ptext-koelectra-v4.0"))


def test_grade_and_index_stay_in_the_same_band():
    for p, level in ((0.0, "SAFE"), (0.29, "SAFE"), (0.3, "WARN"), (0.79, "WARN"), (0.8, "DANGER"), (1.0, "DANGER")):
        assert grade(p, POLICY) == level
        low, high = {"SAFE": (70, 100), "WARN": (40, 69), "DANGER": (0, 39)}[level]
        assert low <= policy_index(p, POLICY) <= high
    assert policy_index(0.0, POLICY) == 100.0 and policy_index(1.0, POLICY) == 0.0


def test_analyze_returns_review_results(client):
    body = {"platform": "kurly", "product_id": "p", "reviews": [
        {"review_id": "a", "content": "0.1"}, {"review_id": "b", "content": "0.5"}, {"review_id": "c", "content": "0.9"}]}
    data = client.post("/v4/analyze", json=body, headers=HEADERS).json()
    assert data["model_version"] == "ptext-koelectra-v4.0" and data["policy_version"] == "text-first-v1"
    assert [r["level"] for r in data["results"]] == ["SAFE", "WARN", "DANGER"]
    assert [r["text_score"] for r in data["results"]] == [90, 50, 10]


def test_auth_duplicates_and_size_limits(client):
    one = {"platform": "k", "product_id": "p", "reviews": [{"review_id": "a", "content": "0.1"}]}
    assert client.post("/v4/analyze", json=one).status_code == 401
    dup = {**one, "reviews": one["reviews"] * 2}
    assert client.post("/v4/analyze", json=dup, headers=HEADERS).status_code == 422
    big = {**one, "reviews": [{"review_id": str(i), "content": "0.1"} for i in range(501)]}
    assert client.post("/v4/analyze", json=big, headers=HEADERS).status_code == 422


def test_unconfigured_model_returns_503():
    body = {"platform": "k", "product_id": "p", "reviews": [{"review_id": "a", "content": "0.1"}]}
    assert make_client().post("/v4/analyze", json=body, headers=HEADERS).status_code == 503


def test_health_is_public_and_reports_versions(client):
    assert client.get("/health").json() == {
        "status": "ok", "model_version": "ptext-koelectra-v4.0", "policy_version": "text-first-v1"}


def test_v4_server_requires_token_and_serves_injected_predictor(monkeypatch):
    from app.v4_server import create_v4_app

    predictor = V4Predictor(fake_probability, POLICY, "ptext-koelectra-v4.0")
    monkeypatch.delenv("INTERNAL_TOKEN", raising=False)
    with pytest.raises(ValueError):
        with TestClient(create_v4_app(predictor)):
            pass
    monkeypatch.setenv("INTERNAL_TOKEN", "synthetic-test")
    body = {"platform": "k", "product_id": "p", "reviews": [{"review_id": "a", "content": "0.9"}]}
    with TestClient(create_v4_app(predictor)) as c:
        assert c.get("/health").status_code == 200
        assert c.post("/v4/analyze", json=body).status_code == 401
        assert c.post("/v4/analyze", json=body, headers=HEADERS).json()["results"][0]["level"] == "DANGER"


def test_v4_server_requires_model_path(monkeypatch):
    from app.v4_server import create_v4_app

    monkeypatch.setenv("INTERNAL_TOKEN", "synthetic-test")
    monkeypatch.delenv("PTEXT_V4_MODEL_PATH", raising=False)
    with pytest.raises(ValueError):
        with TestClient(create_v4_app()):
            pass


def test_policy_file_validation(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({"version": "x", "warn_cut": 0.28, "danger_cut": 0.87}))
    assert V4Policy.load(path) == V4Policy("x", 0.28, 0.87)
    path.write_text(json.dumps({"version": "x", "warn_cut": 0.9, "danger_cut": 0.5}))
    with pytest.raises(ValueError):
        V4Policy.load(path)
