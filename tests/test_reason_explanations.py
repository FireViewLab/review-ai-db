"""빈 근거 안내만 추가하고 점수·Groq·저장·과거 replay 계약을 보존한다."""

from copy import deepcopy
from dataclasses import FrozenInstanceError
import json
from unittest.mock import MagicMock, Mock

from fastapi.testclient import TestClient
import pymysql
import pytest

from app.contracts.data_ai_v05 import DataAnalyzeRequestV05, DataReviewResultV05
from app.core.analysis_versions import analysis_versions
from app.factory import create_app
from app.repositories.analysis_jobs import SQLiteJobStore
from app.repositories.mysql_jobs import MySQLJobStore
from app.services import data_analysis
from app.services.reason_explanations import (
    DANGER_MESSAGE, EXPLANATION_POLICY_VERSION, PARTIAL_SIGNALS_MESSAGE,
    SAFE_MESSAGE, UNAVAILABLE_MESSAGE, WARN_MESSAGE,
    build_explanation_context, neutral_reasons,
)
from tests.test_data_analysis_stream import decode


def result(score=80, level="safe", reasons=()):
    return DataReviewResultV05(review_id="review-1", rti=score, level=level,
                              text_score=score, behavior_score=-1, network_score=-1,
                              reasons=list(reasons))


@pytest.mark.parametrize("score,level,message", [
    (0, "danger", DANGER_MESSAGE), (39.9, "danger", DANGER_MESSAGE),
    (40, "warn", WARN_MESSAGE), (69.9, "warn", WARN_MESSAGE),
    (70, "safe", SAFE_MESSAGE), (100, "safe", SAFE_MESSAGE),
])
def test_neutral_notice_follows_existing_level_without_changing_scores(score, level, message):
    original = result(score, level)
    before = original.model_dump()
    context = build_explanation_context(original)
    assert neutral_reasons(context) == [message, PARTIAL_SIGNALS_MESSAGE]
    assert original.model_dump() == before
    assert context.rti == score and context.level == level
    assert [signal.available for signal in context.signals] == [True, False, False]
    assert context.signals[0].score == score  # A genuine zero remains available.


def test_all_unavailable_has_only_unavailable_notice_and_preserves_sentinel():
    original = result(-1, None)
    context = build_explanation_context(original)
    assert neutral_reasons(context) == [UNAVAILABLE_MESSAGE]
    assert context.rti == -1 and context.level is None
    assert all(signal.score == -1 and not signal.available for signal in context.signals)


def test_all_available_has_no_missing_signal_notice():
    original = result().model_copy(update={"behavior_score": 80, "network_score": 80})
    assert neutral_reasons(build_explanation_context(original)) == [SAFE_MESSAGE]


def test_raw_reason_context_is_immutable_and_preserves_code_order():
    codes = ["TEXT_SHORT_REVIEW", "NETWORK_SIMILAR_REVIEW_PATTERN"]
    original = result(reasons=codes)
    context = build_explanation_context(original)
    original.reasons.clear()
    assert context.raw_reason_codes == tuple(codes)
    assert neutral_reasons(context) == codes
    assert context.explanation_policy_version == EXPLANATION_POLICY_VERSION
    with pytest.raises(FrozenInstanceError):
        context.rti = 0
    with pytest.raises(FrozenInstanceError):
        context.signals[0].score = 0


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setenv("ENABLE_EXPERIMENTAL_COLLECTION", "0")
    monkeypatch.setenv("ENABLE_GROQ_REASON_NATURALIZATION", "0")
    monkeypatch.setenv("REQUIRE_INTERNAL_TOKEN", "0")
    monkeypatch.delenv("INTERNAL_TOKEN", raising=False)
    store = SQLiteJobStore(str(tmp_path / "explanations.db"))
    with TestClient(create_app(job_store=store)) as http:
        yield http, store


def payload():
    return {"platform": "mall", "product_id": "product-1", "reviews": [
        {"review_id": "review-1", "content": "제품을 한 달 사용하면서 확인한 내용을 정리한 리뷰입니다."},
        {"review_id": "review-2", "content": "짧은 리뷰"},
    ]}


def evaluated():
    return {"platform": "mall", "product_id": "product-1", "review_count": 2, "results": [
        result().model_dump(mode="json"),
        {**result(45, "warn", ["TEXT_SHORT_REVIEW"]).model_dump(mode="json"), "review_id": "review-2"},
    ]}


def assert_scores_and_shape_unchanged(before, after):
    assert set(after) == set(before) == {"platform", "product_id", "review_count", "results"}
    assert {key: value for key, value in after.items() if key != "results"} == {
        key: value for key, value in before.items() if key != "results"
    }
    for old, new in zip(before["results"], after["results"], strict=True):
        assert set(old) == set(new)
        assert {key: value for key, value in old.items() if key != "reasons"} == {
            key: value for key, value in new.items() if key != "reasons"
        }


@pytest.mark.parametrize("groq_failure", [False, True])
def test_empty_raw_reason_notice_survives_groq_and_preserves_existing_codes(api, monkeypatch, groq_failure):
    http, store = api
    raw = evaluated()
    monkeypatch.setattr(data_analysis, "analyze_reviews", lambda **kwargs: deepcopy(raw))
    calls = []

    def naturalize(*, contents, reasons):
        calls.append(deepcopy(reasons))
        assert reasons == [[], ["TEXT_SHORT_REVIEW"]]
        if groq_failure:
            raise RuntimeError("synthetic provider failure")
        return [[], ["리뷰 내용이 짧아 추가적인 맥락을 확인하기 어렵습니다."]]

    monkeypatch.setattr(data_analysis, "naturalize_reasons_batch", naturalize)
    response = http.post("/api/v1/data/analyze", json=payload())
    assert response.status_code == 200
    final = response.json()
    assert_scores_and_shape_unchanged(raw, final)
    assert final["results"][0]["reasons"] == [SAFE_MESSAGE, PARTIAL_SIGNALS_MESSAGE]
    expected = ["TEXT_SHORT_REVIEW"] if groq_failure else ["리뷰 내용이 짧아 추가적인 맥락을 확인하기 어렵습니다."]
    assert final["results"][1]["reasons"] == expected
    assert store.get(response.headers["x-analysis-job-id"])["result"] == final
    assert calls == [[[], ["TEXT_SHORT_REVIEW"]]]
    assert raw["results"][0]["reasons"] == []


@pytest.mark.parametrize("score,level,expected", [
    (-1, None, [UNAVAILABLE_MESSAGE]),
    (0, "danger", [DANGER_MESSAGE, PARTIAL_SIGNALS_MESSAGE]),
])
def test_api_unavailable_and_real_zero_keep_original_values(api, monkeypatch, score, level, expected):
    http, store = api
    body = payload()
    body["reviews"] = body["reviews"][:1]
    raw = {"platform": body["platform"], "product_id": body["product_id"], "review_count": 1,
           "results": [result(score, level).model_dump(mode="json")]}
    monkeypatch.setattr(data_analysis, "analyze_reviews", lambda **kwargs: deepcopy(raw))
    response = http.post("/api/v1/data/analyze", json=body)
    assert response.status_code == 200
    final = response.json()
    assert_scores_and_shape_unchanged(raw, final)
    assert final["results"][0]["reasons"] == expected
    assert store.get(response.headers["x-analysis-job-id"])["result"] == final


def test_api_sse_storage_and_replay_have_same_neutral_reasons(api, monkeypatch):
    http, store = api
    analyze = Mock(side_effect=lambda **kwargs: deepcopy(evaluated()))
    monkeypatch.setattr(data_analysis, "analyze_reviews", analyze)
    normal = http.post("/api/v1/data/analyze", json=payload())
    headers = {"X-Request-ID": "notice-request", "Idempotency-Key": "notice-job"}
    stream = http.post("/api/v1/data/analyze/stream", json=payload(), headers=headers)
    assert normal.status_code == stream.status_code == 200
    events = decode(stream.text)
    rows = [dict(data) for name, data in events if name == "result"]
    for row in rows:
        row.pop("request_id")
    assert rows == normal.json()["results"]
    assert events[-1][0] == "done"
    for reply in (normal, stream):
        assert store.get(reply.headers["x-analysis-job-id"])["result"] == normal.json()
    assert analyze.call_count == 2

    forbidden = Mock(side_effect=AssertionError("Replay must not regenerate explanations"))
    monkeypatch.setattr(data_analysis, "neutral_reasons", forbidden)
    replay = http.post("/api/v1/data/analyze/stream", json=payload(), headers=headers)
    assert replay.status_code == 200
    assert [name for name, _ in decode(replay.text)] == ["meta", "result", "result", "done"]
    assert replay.headers["x-analysis-job-id"] == stream.headers["x-analysis-job-id"]
    assert [data for name, data in decode(replay.text) if name == "result"] == [
        data for name, data in events if name == "result"
    ]
    assert analyze.call_count == 2
    forbidden.assert_not_called()


def test_old_done_empty_reasons_are_replayed_without_regeneration(api, monkeypatch):
    http, store = api
    body = payload()
    claim = store.claim_idempotent("old-empty-reasons", DataAnalyzeRequestV05.model_validate(body).model_dump(mode="json"), analysis_versions())
    old = evaluated()
    store.complete(claim.job_id, old)
    forbidden = Mock(side_effect=AssertionError("Old result must remain unchanged"))
    monkeypatch.setattr(data_analysis, "analyze_reviews", forbidden)
    monkeypatch.setattr(data_analysis, "neutral_reasons", forbidden)
    response = http.post("/api/v1/data/analyze/stream", json=body, headers={
        "X-Request-ID": "old-retry", "Idempotency-Key": "old-empty-reasons",
    })
    events = decode(response.text)
    assert response.status_code == 200 and events[-1][0] == "done"
    assert next(data for name, data in events if name == "result")["reasons"] == []
    assert store.get(claim.job_id)["result"] == old
    forbidden.assert_not_called()


def test_neutral_explanation_does_not_hide_storage_failure(api, monkeypatch):
    http, store = api
    monkeypatch.setattr(data_analysis, "analyze_reviews", lambda **kwargs: evaluated())
    monkeypatch.setattr(store, "complete", Mock(side_effect=OSError("synthetic DB failure")))
    response = http.post("/api/v1/data/analyze", json=payload())
    assert response.status_code == 503 and "results" not in response.json()


def test_mysql_receives_same_final_reason_json_without_schema_changes(monkeypatch):
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setenv("ENABLE_GROQ_REASON_NATURALIZATION", "0")
    db = MagicMock()
    monkeypatch.setattr(pymysql, "connect", lambda **kwargs: db)
    store = MySQLJobStore(host="validation-only", port=3306, user="synthetic-user",
                          password="synthetic-password", database="validation-only")
    raw = evaluated()
    monkeypatch.setattr(data_analysis, "analyze_reviews", lambda **kwargs: deepcopy(raw))
    final = data_analysis.evaluate_data_and_store(store, "test-job", DataAnalyzeRequestV05.model_validate(payload()))
    calls = db.cursor.return_value.__enter__.return_value.execute.call_args_list
    updates = [call.args for call in calls if "result_json=%s" in call.args[0]]
    assert len(updates) == 1
    sql, params = updates[0]
    assert SAFE_MESSAGE not in sql
    assert json.loads(params[0]) == final.model_dump(mode="json")
    assert final.results[0].reasons == [SAFE_MESSAGE, PARTIAL_SIGNALS_MESSAGE]
    assert_scores_and_shape_unchanged(raw, final.model_dump(mode="json"))
