"""Local-only evidence reason wrapper. No provider network calls or model loads."""

import json

import httpx
import pytest

from app.experimental.candidate_analysis import _REASON_MESSAGES
from app.integrations.groq_reason_naturalizer import GroqReasonSettings
from scripts.compare_ai_v3_groq_reasons import MOCK_SCENARIOS, ReasonCase, compare_cases, load_cases, mock_run


@pytest.fixture
def cases():
    cases = []
    for i in range(1, 13):
        codes = ("MEANINGFUL_TEXT_0_9_STRONG_PENALTY", "IMAGE_COUNT_UNKNOWN", "RATING_UNKNOWN",
                 "EXPERIENCE_EVIDENCE_NO", "PROMOTIONAL_STYLE_NOT_DETECTED", "PHOTO_EVIDENCE_UNKNOWN")
        score = 82 if i == 1 else 21 if i == 2 else 52
        level = "SAFE" if i == 1 else "DANGER" if i == 2 else "WARN"
        cases.append(ReasonCase(f"T{i:02d}", f"합성 리뷰 {i}", score, level, codes,
                                tuple(_REASON_MESSAGES[code] for code in codes)))
    return tuple(cases)


@pytest.mark.parametrize("scenario", MOCK_SCENARIOS)
def test_twelve_reviews_use_real_converter_but_only_mock_transport(cases, scenario):
    result = mock_run(cases, scenario)
    ids = [case.review_id for case in cases]
    assert result["requested_review_ids"] and all(request == ids for request in result["requested_review_ids"])
    assert all(result["request_contract_ok"])
    assert [row["review_id"] for row in result["rows"]] == ids
    for case, row in zip(cases, result["rows"], strict=True):
        assert row["policy_index_v3"] == case.policy_index_v3
        assert row["evidence_level_v3"] == case.evidence_level_v3
        assert row["evidence_reasons_v3"] == list(case.codes)
        assert row["display_reasons_ko"] == row["rules_reasons_ko"] == list(case.templates)
        assert row["groq_reasons_ko"] is None
        assert case.content not in json.dumps(row, ensure_ascii=False)
    if scenario in ("success", "rate_limit_recovery"):
        assert all(row["status"] == "offline_review_only" and row["meaning_matches"]
                   and row["unsupported_or_exaggerated"] is False for row in result["rows"])
    elif scenario == "unsafe_language":
        bad_ids = {row["review_id"] for row in result["rows"] if row["unsupported_or_exaggerated"]}
        assert bad_ids == {"T01", "T05"}
        assert any("실제 구매" in line for line in result["rows"][0]["mock_reasons_ko"])
        assert all("실제 구매" not in line for line in result["rows"][0]["display_reasons_ko"])
        assert all("사진이 없" not in line for line in result["rows"][4]["display_reasons_ko"])
    else:
        assert all(row["status"] == "rules_fallback" and row["mock_reasons_ko"] is None
                   and row["meaning_matches"] is None for row in result["rows"])
    if scenario in ("rate_limited", "rate_limit_recovery", "timeout"):
        assert result["used_secondary"] == [False, True]
    else:
        assert result["used_secondary"] == [False]


def test_requires_explicit_transport_and_small_live_subset(cases):
    settings = GroqReasonSettings()
    with pytest.raises(ValueError, match="explicit transport"):
        compare_cases(cases, mode="mock", settings=settings, transport=None)
    with pytest.raises(ValueError, match="batch size"):
        compare_cases(cases, mode="live", settings=settings, transport=object())


def test_live_style_result_requires_manual_review_and_does_not_change_display(cases):
    settings = GroqReasonSettings(True, "test-only-primary", None, "test-only-model")
    message = "검증된 사진 정보는 제공되지 않았습니다."

    def fake(request):
        body = json.loads(request.content)
        items = json.loads(body["messages"][-1]["content"])["items"]
        content = json.dumps({"items": [
            {"item_id": item["item_id"], "reasons": [{"code": code, "message": message} for code in item["reasons"]]}
            for item in items
        ]}, ensure_ascii=False)
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": content}}]})

    row = compare_cases(cases[:1], mode="live", settings=settings, transport=httpx.MockTransport(fake))[0]
    assert row["mock_reasons_ko"] is None and row["groq_reasons_ko"] == [message] * len(cases[0].codes)
    assert row["display_reasons_ko"] == list(cases[0].templates)
    assert row["review_status"] == "requires_semantic_review"
    assert row["meaning_matches"] is row["unsupported_or_exaggerated"] is None


@pytest.mark.parametrize("message", ["IMAGE_COUNT_UNKNOWN", "테스트용 모델 TEST-ONLY-MODEL입니다."])
def test_untranslated_code_or_model_name_leads_to_whole_review_fallback(cases, message):
    settings = GroqReasonSettings(True, "test-only-primary", None, "test-only-model")

    def fake(request):
        case = cases[0]
        reasons = [{"code": code, "message": message if i == 1 else case.templates[i]}
                   for i, code in enumerate(case.codes)]
        content = json.dumps({"items": [{"item_id": "0", "reasons": reasons}]}, ensure_ascii=False)
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": content}}]})

    row = compare_cases(cases[:1], mode="live", settings=settings, transport=httpx.MockTransport(fake))[0]
    assert row["groq_reasons_ko"] is None and row["status"] == "rules_fallback"
    assert row["display_reasons_ko"] == list(cases[0].templates)


def test_foreign_fixture_is_rejected_before_report_is_read(tmp_path):
    fixture = tmp_path / "unverified.json"
    fixture.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        load_cases(fixture, tmp_path / "not-read.json")
