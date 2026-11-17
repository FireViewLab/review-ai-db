"""Local candidate gates, provenance, batch ordering, and no changes to the v0.5 API."""

from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.analyzers import p_text
from app.experimental.candidate_analysis import (
    CANDIDATE_FLAG, WEB_PREVIEW_FLAG, CandidateBatch, analyze_candidate_batch,
)
from app.factory import create_app
from app.repositories.analysis_jobs import SQLiteJobStore
from tests.test_data_analysis_stream import decode


def review(**overrides):
    return {"platform": "synthetic", "product_id": "product-01", "review_id": "01",
            "content": "좋아요", **overrides}


def batch(*rows):
    return CandidateBatch.model_validate({"reviews": rows})


def predicted(score, label="NORMAL"):
    return dict(text_score=score, predicted_label=label, suspicious_probability=0.35 if label == "NORMAL" else 0.6)


def test_candidate_disabled_by_default_even_with_web_preview(monkeypatch):
    monkeypatch.delenv(CANDIDATE_FLAG, raising=False)
    monkeypatch.setenv(WEB_PREVIEW_FLAG, "1")
    scorer = Mock()
    with pytest.raises(RuntimeError, match=CANDIDATE_FLAG):
        analyze_candidate_batch(batch(review()), text_predictor=scorer)
    scorer.assert_not_called()


def test_candidate_has_one_text_batch_and_preserves_order_and_unknowns(monkeypatch):
    monkeypatch.setenv(CANDIDATE_FLAG, "1")
    monkeypatch.delenv(WEB_PREVIEW_FLAG, raising=False)
    inputs = batch(review(review_id="002"), review(review_id="001", content="포장이 깔끔합니다"))
    scorer = Mock(return_value=[predicted(59), predicted(62)])
    response = analyze_candidate_batch(inputs, text_predictor=scorer)
    scorer.assert_called_once_with([r.content for r in inputs.reviews])
    assert [row["review_id"] for row in response["results"]] == ["002", "001"]
    for row, score in zip(response["results"], (59, 62), strict=True):
        assert row["original_rti"] == row["original_v05"]["text_score"] == score
        assert row["original_level"] == "warn"
        assert row["original_v05"]["behavior_score"] == -1
        assert row["policy_v3"]["image_evidence_state"] == "UNKNOWN"
        assert {"rating", "image_count", "behavior_evidence"} <= set(row["policy_v3"]["missing_information"])
        assert row["policy_v3"]["evidence_level_v3"] == "WARN"
        assert "proposed_web_preview" not in row


def test_web_preview_is_separate_from_original_and_keeps_signal_scores(monkeypatch):
    monkeypatch.setenv(CANDIDATE_FLAG, "1")
    monkeypatch.setenv(WEB_PREVIEW_FLAG, "1")
    inputs = batch(review(rating=5, rating_scale_max=5, rating_source="synthetic_explicit",
                          rating_source_verified=True, image_count=0, image_count_source="synthetic_explicit",
                          image_count_source_verified=True))
    result = analyze_candidate_batch(inputs, text_predictor=lambda texts: [predicted(55)])["results"][0]
    assert result["original_rti"] == result["original_v05"]["rti"] == 55
    assert result["original_level"] == result["original_v05"]["level"] == "warn"
    policy = result["policy_v3"]
    preview = result["proposed_web_preview"]
    assert preview["rti"] == policy["policy_index_v3"]
    assert preview["level"] == policy["evidence_level_v3"].lower() == "danger"
    assert preview["score_interpretation"] == "ORDINAL_V3_POLICY_INDEX_NOT_PROBABILITY"
    assert all(preview[field] == result["original_v05"][field]
               for field in ("text_score", "behavior_score", "network_score"))
    assert len(preview["reasons"]) == len(policy["evidence_reasons_v3"])
    assert any("사진" in reason for reason in preview["reasons"])
    assert not any("가짜" in reason or "구매 사실" in reason for reason in preview["reasons"])


@pytest.mark.parametrize("change", [
    {"rating": 5}, {"image_count": 0}, {"rating_source_verified": True},
    {"image_count_source_verified": True}, {"behavior": {"nickname": "display-only"}},
])
def test_experimental_input_rejects_unverified_or_unsupported_evidence(change):
    with pytest.raises(ValidationError):
        batch(review(**change))


@pytest.mark.parametrize("change", [{"platform": "another"}, {"product_id": "another"}, {"review_id": "01"}])
def test_product_batch_rejects_mixed_identity_and_duplicate_ids(change):
    with pytest.raises(ValidationError):
        batch(review(), {**review(review_id="02"), **change})


def test_missing_rti_does_not_create_web_policy(monkeypatch):
    monkeypatch.setenv(CANDIDATE_FLAG, "1")
    monkeypatch.setenv(WEB_PREVIEW_FLAG, "1")
    row = analyze_candidate_batch(
        batch(review(content=" ")), text_predictor=lambda texts: [p_text.unavailable_result()],
    )["results"][0]
    assert row["original_rti"] is None and row["original_level"] is None
    assert row["original_v05"]["rti"] == -1 and row["original_v05"]["level"] is None
    assert row["policy_v3"] is None and row["proposed_web_preview"] is None
    assert row["policy_unavailable_reason"] == "original_rti_unavailable"


def test_failed_candidate_inference_on_text_is_not_missing_evidence(monkeypatch):
    monkeypatch.setenv(CANDIDATE_FLAG, "1")
    with pytest.raises(RuntimeError, match="Candidate inference failed"):
        analyze_candidate_batch(batch(review()), text_predictor=lambda texts: [p_text.unavailable_result()])


def test_flags_do_not_change_openapi_json_sse_replay_or_persisted_result(tmp_path, monkeypatch, model_free_prediction):
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setenv("ENABLE_EXPERIMENTAL_COLLECTION", "0")
    monkeypatch.setenv("ENABLE_GROQ_REASON_NATURALIZATION", "0")
    monkeypatch.setenv("REQUIRE_INTERNAL_TOKEN", "0")
    monkeypatch.delenv("INTERNAL_TOKEN", raising=False)
    monkeypatch.delenv(CANDIDATE_FLAG, raising=False)
    monkeypatch.delenv(WEB_PREVIEW_FLAG, raising=False)
    db = SQLiteJobStore(str(tmp_path / "isolated.db"))
    payload = {"platform": "synthetic", "product_id": "product-01",
               "reviews": [{"review_id": "01", "content": "배송 상태가 좋아 만족합니다."}]}
    path = "/api/v1/data/analyze"
    with TestClient(create_app(job_store=db)) as client:
        before_schema = client.get("/openapi.json").json()
        before = client.post(path, json=payload).json()
    monkeypatch.setenv(CANDIDATE_FLAG, "1")
    monkeypatch.setenv(WEB_PREVIEW_FLAG, "1")
    monkeypatch.setenv("PTEXT_V31B_MODEL_PATH", "must-not-be-loaded-by-server")
    forbidden = Mock(side_effect=AssertionError("Production API must not load the candidate"))
    monkeypatch.setattr(p_text, "load_candidate_predictor", forbidden)
    with TestClient(create_app(job_store=db)) as client:
        assert client.get("/openapi.json").json() == before_schema
        assert client.post(path, json=payload).json() == before
        for new_fields in ({"image_count": 1}, {"rating_source_verified": True}, {"behavior": {}}):
            expanded = {**payload, "reviews": [{**payload["reviews"][0], **new_fields}]}
            assert client.post(path, json=expanded).status_code == 422
        headers = {"X-Request-ID": "test-candidate", "Idempotency-Key": "test-isolation"}
        stream = client.post(path + "/stream", json=payload, headers=headers)
        assert stream.status_code == 200
        job_id = stream.headers["X-Analysis-Job-ID"]
        assert db.get(job_id)["result"] == before
        replay = client.post(path + "/stream", json=payload, headers=headers)
        assert replay.headers["X-Analysis-Job-ID"] == job_id
        assert decode(replay.text) == [(name, data) for name, data in decode(stream.text) if name != "progress"]
        wire_results = [{key: value for key, value in data.items() if key != "request_id"}
                        for name, data in decode(stream.text) if name == "result"]
        assert wire_results == before["results"]
    forbidden.assert_not_called()
