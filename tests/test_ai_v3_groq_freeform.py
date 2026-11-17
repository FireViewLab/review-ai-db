"""Freeform A/B request fidelity and containment, using no provider network."""

import asyncio
from copy import deepcopy
from dataclasses import replace
import json

import httpx
import pytest

from app.experimental.candidate_analysis import _REASON_MESSAGES
from app.experimental.summary_candidate import SUMMARY_CANDIDATE_VERSION, approved_summary_options, select_approved_summary
from app.integrations.groq_reason_naturalizer import GroqReasonSettings, _request_payload
from scripts import compare_ai_v3_groq_freeform as summary_cli
from scripts.ai_v3_groq_free_text_experiment import audit_free_text_response, free_text_payload
from scripts.compare_ai_v3_groq_freeform import (
    MAX_RATE_LIMIT_ERROR_BYTES, RECONSTRUCTED_PROMPT, check_evidence, compare_temperatures, parse_output, payloads,
    request_items, resume_arms, system_prompt,
)
from scripts.compare_ai_v3_groq_reasons import LIVE_REVIEW_IDS, ReasonCase


@pytest.fixture
def cases():
    photo_rating = (
        ("IMAGE_PRESENT_VERIFIED", "RATING_FIVE_VERIFIED", "VERIFIED_PHOTO_AND_NO_PROMOTIONAL_STYLE"),
        ("IMAGE_ABSENT_VERIFIED", "RATING_FIVE_VERIFIED", "VERIFIED_NO_PHOTO_ULTRASHORT_FIVE_STAR"),
        ("IMAGE_COUNT_UNKNOWN", "RATING_UNKNOWN", "PHOTO_EVIDENCE_UNKNOWN"),
        ("IMAGE_ABSENT_VERIFIED", "RATING_FIVE_VERIFIED", "SAFE_REQUIREMENTS_INCOMPLETE"),
    )
    full_evidence = tuple(("MEANINGFUL_TEXT_0_9_STRONG_PENALTY" if i < 3 else "MEANINGFUL_TEXT_10_29_GENERAL_PENALTY",
                           photo, rating, "EXPERIENCE_EVIDENCE_NO", "PROMOTIONAL_STYLE_NOT_DETECTED", decision)
                          for i, (photo, rating, decision) in enumerate(photo_rating))
    return tuple(ReasonCase(review_id, f"합성 본문 {index}", 80.4, "SAFE", codes,
                            tuple(_REASON_MESSAGES[code] for code in codes))
                 for index, (review_id, codes) in enumerate(zip(LIVE_REVIEW_IDS, full_evidence, strict=True)))


@pytest.fixture
def settings():
    return GroqReasonSettings(True, "test-only-primary", "test-only-secondary", "test-only-model")


def envelope(groups=None):
    groups = groups if groups is not None else ["테스트 문장입니다." for _ in range(4)]
    return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
        "items": [{"item_id": str(index), "analysis_summary_ko": summary} for index, summary in enumerate(groups)],
    }, ensure_ascii=False)}}], "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150,
                                         "unsafe_extra": "not persisted"}}


def test_only_temperature_changes_and_no_scores_or_real_ids_leak(cases, settings):
    requests = payloads(cases, prompt="검토용 원문", model=settings.model)
    assert [request.pop("temperature") for request in requests] == [0.0, 0.3, 0.5]
    assert requests[0] == requests[1] == requests[2]
    body = requests[0]
    assert body["top_p"] == 1 and "frequency_penalty" not in body
    assert body["max_completion_tokens"] == 1536
    assert _request_payload([{"item_id": "0", "reasons": ["example"]}], settings)["max_completion_tokens"] == 4096
    assert body["messages"][0]["content"].startswith("검토용 원문")
    assert body["response_format"]["json_schema"]["strict"] is True
    shape = body["response_format"]["json_schema"]["schema"]["properties"]["items"]["items"]
    assert shape["properties"]["analysis_summary_ko"] == {"type": "string"}
    items = json.loads(body["messages"][1]["content"])["items"]
    for i, item in enumerate(items):
        assert set(item) == {"item_id", "approved_summaries_ko"}
        assert item["item_id"] == str(i)
        assert item["approved_summaries_ko"] == list(approved_summary_options(cases[i].codes))
        assert len(item["approved_summaries_ko"]) == 2
        assert cases[i].content not in body["messages"][1]["content"]
    assert "T05" not in json.dumps(items)


def test_reject_wrong_case_group(cases):
    with pytest.raises(ValueError, match="approved"):
        request_items(tuple(reversed(cases)))


def test_historical_free_text_protocol_is_separate_and_never_accepted_for_display(cases):
    request = free_text_payload(cases, prompt=RECONSTRUCTED_PROMPT)
    items = json.loads(request["messages"][1]["content"])["items"]
    assert request["temperature"] == 0.3 and request["model"] == "openai/gpt-oss-20b"
    assert items[0]["review_content"] == cases[0].content and items[0]["must_preserve_ko"]
    assert "approved_summaries_ko" not in items[0] and request_items(cases)[0].get("review_content") is None
    risky = "사진이 있으므로 실제 사용한 리뷰입니다."
    report = audit_free_text_response(cases, json.dumps(envelope([risky] * 4)).encode())
    for case in cases:
        row = report["rows"][case.review_id]
        assert row["experimental_analysis_summary_ko"] == risky
        assert row["display_reasons_ko"] == list(case.templates)
        assert row["evidence_check"]["display_source"] == "rules_reasons_ko"


@pytest.mark.parametrize("index,mandatory", [
    (0, ("사진이 확인되고", "홍보성 표현 조합이 감지되지 않아", "두 조건이 함께")),
    (1, ("사진이 0장", "별점이 5점으로 확인", "의미 있는 본문이 10자 미만", "세 조건이 함께")),
    (2, ("사진 개수와 별점 정보는 확인되지", "사진 조건은 판정에 사용하지")),
    (3, ("의미 있는 본문은 10~29자", "길이 감점", "정책 조건이 모두 충족됐다고 확인하기 어려웠습니다")),
])
def test_every_option_preserves_exact_policy_claims(cases, index, mandatory):
    options = approved_summary_options(cases[index].codes)
    assert len(options) == 2 and options[0] != options[1]
    assert all("\n" not in text and all(claim in text for claim in mandatory) for text in options)
    assert index != 0 or all("5점" not in text for text in options)


def test_unreviewed_or_contradictory_evidence_is_closed(cases):
    original = cases[0].codes
    for codes in (original[:-1], (*original[:-1], "IMAGE_ABSENT_VERIFIED", original[-1]),
                  (*original, original[-1]), (*original[:-1], "ORIGINAL_RTI_DANGER_PRESERVED")):
        assert approved_summary_options(codes) == ()
        with pytest.raises(ValueError, match="Unsupported evidence"):
            request_items((replace(cases[0], codes=codes), *cases[1:]))


def test_offline_preview_before_any_service_configuration(tmp_path, monkeypatch, cases):
    output = tmp_path / "preview.json"
    baseline = [{"review_id": case.review_id, "display_reasons_ko": list(case.templates)} for case in cases]
    monkeypatch.setattr(summary_cli, "LOCAL_OUTPUT", tmp_path)
    monkeypatch.setattr(summary_cli, "load_cases", lambda *args: cases)
    monkeypatch.setattr(summary_cli, "comparison_rows", lambda *args: baseline)

    def unexpected_service_access(*args, **kwargs):
        pytest.fail("Offline preview must not load keys or call the service")

    monkeypatch.setattr(summary_cli, "load_settings", unexpected_service_access)
    monkeypatch.setattr(summary_cli, "compare_temperatures", unexpected_service_access)
    monkeypatch.setattr("dotenv.load_dotenv", unexpected_service_access)
    monkeypatch.setattr("sys.argv", ["preview", "--offline-preview", "--fixture", "fixture", "--comparison", "baseline",
                                     "--prior-live", "previous", "--output", str(output)])
    assert summary_cli.main() == 0
    data = json.loads(output.read_text(encoding="utf-8"))
    assert data["actual_requests"] == 0 and data["temperature"] == 0.3
    for case, row in zip(cases, data["rows"], strict=True):
        assert row["analysis_summary_ko"] == approved_summary_options(case.codes)[0]
        assert row["display_source"] == "rules_reasons_ko" and row["display_reasons_ko"] == list(case.templates)


def test_prompt_replaces_only_output_directions():
    before = "## 1. 표현의 자율성\n사실만 작성하세요."
    after = "최종 작성 전에 다음을 확인하세요.\n- 사실인가요?"
    result = system_prompt(f"{before}\n## 7. 출력 형식\n\ndisplay_reasons_ko\n\n{after}")
    assert result.startswith(before)
    assert after in result
    assert "analysis_summary_ko" in result and "display_reasons_ko" not in result


@pytest.mark.parametrize("index,generator_summary", [
    (0, "사진이 확인되었고 홍보성 표현 조합이 감지되지 않아 두 조건이 함께 긍정 근거가 됩니다."),
    (1, "사진이 0장이고 확인된 별점이 5점이며 의미 있는 본문이 10자 미만이라는 세 조건이 함께 확인됩니다."),
    (2, "사진 개수와 별점은 확인되지 않았습니다. 사진 조건은 판정에 반영하지 않았습니다."),
    (3, "의미 있는 본문이 10~29자라 길이 감점이 반영되었습니다. 긍정 조건 충족 여부는 확인하기 어렵습니다."),
])
def test_anchors_match_but_cannot_approve_for_display(cases, index, generator_summary):
    result = check_evidence(generator_summary, cases[index].codes)
    assert result["unverified_codes"] == result["inference_flags"] == []
    assert result["status"] == "human_review_required"
    assert result["display_source"] == "rules_reasons_ko"


@pytest.mark.parametrize("index,generator_summary,missing", [
    (0, "사진이 확인되었고 별점 5점이 긍정 근거입니다.", "VERIFIED_PHOTO_AND_NO_PROMOTIONAL_STYLE"),
    (1, "별점은 5점으로 확인했습니다. 사진 부재와 의미 있는 본문 10자 미만이 주의 조합입니다.", "VERIFIED_NO_PHOTO_ULTRASHORT_FIVE_STAR"),
    (2, "사진과 별점 정보는 확인되지 않았습니다.", "PHOTO_EVIDENCE_UNKNOWN"),
    (3, "본문은 10~29자입니다. 긍정 조건 충족 여부는 확인하기 어렵습니다.", "MEANINGFUL_TEXT_10_29_GENERAL_PENALTY"),
])
def test_missing_or_disconnected_determinative_evidence_requires_fallback(cases, index, generator_summary, missing):
    result = check_evidence(generator_summary, cases[index].codes)
    assert result["status"] == "needs_fallback"
    assert missing in result["unverified_codes"]
    assert result["display_source"] == "rules_reasons_ko"


def test_known_overreach_triggered_even_with_positive_anchor_present(cases):
    result = check_evidence(
        "사진이 확인되었고 홍보성 표현 조합이 감지되지 않아 함께 긍정 근거가 됩니다. "
        "사진이 실제 사용을 기반으로 했음을 시사합니다. 실제 사용 경험이 부족합니다.", cases[0].codes,
    )
    assert result["unverified_codes"] == []
    assert result["inference_flags"] == ["photo_implies_purchase_or_use", "undocumented_experience_treated_as_absent"]
    assert result["status"] == "needs_fallback"


def test_literal_claims_of_absent_experience_or_promotion_are_not_approved(cases):
    risky = "구체적인 사용 경험이 없고 홍보성 표현이 없습니다."
    checked = check_evidence(risky, cases[2].codes)
    assert checked["inference_flags"] == ["undocumented_experience_treated_as_absent",
                                           "promotional_pattern_miss_treated_as_absence"]
    careful = "본문에 구체적인 사용 경험이 드러나지 않고 홍보성 표현 조합이 감지되지 않았습니다."
    assert check_evidence(careful, cases[2].codes)["inference_flags"] == []


def test_resume_stops_after_429_and_preserves_successful_arm():
    expected = {"experiment": "local_only", "effective_prompt_sha256": "same",
                "summary_candidate_version": SUMMARY_CANDIDATE_VERSION,
                "source_prompt": "provided", "rows": [], "top_p": 1, "actual_requests": 1,
                "max_completion_tokens": 1536,
                "arms": [{"temperature": 0.0, "attempts": 0, "http_status": None, "status": "not_requested"},
                         {"temperature": 0.3, "attempts": 1, "http_status": 200, "status": "format_passed",
                          "max_completion_tokens": 1536, "analysis_summary_ko": {"T01": "설명입니다."}},
                         {"temperature": 0.5, "attempts": 0, "http_status": None, "status": "not_requested"}]}
    previous = resume_arms(expected, prompt_digest="same", prompt_source="provided", baseline=[])
    assert list(previous) == [0.3]
    with pytest.raises(ValueError, match="does not match"):
        resume_arms(expected, prompt_digest="different", prompt_source="provided", baseline=[])
    variants = [dict(expected, max_completion_tokens=4096),
                {key: value for key, value in expected.items() if key != "max_completion_tokens"},
                dict(expected, arms=[expected["arms"][0], dict(expected["arms"][1], max_completion_tokens=4096),
                                     expected["arms"][2]])]
    for incompatible in variants:
        with pytest.raises(ValueError, match="completion cap"):
            resume_arms(incompatible, prompt_digest="same", prompt_source="provided", baseline=[])
    expected["arms"][1].update(http_status=429, status="http_rejected", analysis_summary_ko=None)
    with pytest.raises(ValueError, match="unsuccessful"):
        resume_arms(expected, prompt_digest="same", prompt_source="provided", baseline=[])


@pytest.mark.parametrize("status", [None, 400, 429, "timeout", "invalid_json"])
def test_no_retry_no_second_call_after_error(cases, settings, status):
    requests = []

    def fake(request):
        request_data = json.loads(request.content)
        requests.append(request_data["temperature"])
        if status == "timeout":
            raise httpx.ReadTimeout("mock")
        if status == "invalid_json":
            return httpx.Response(200, text="{")
        items = json.loads(request_data["messages"][1]["content"])["items"]
        return httpx.Response(status or 200, json=envelope([item["approved_summaries_ko"][1] for item in items]),
                              headers={"x-ratelimit-remaining-tokens": "7412"})

    results = asyncio.run(compare_temperatures(cases, prompt=RECONSTRUCTED_PROMPT, settings=settings,
                                               transport=httpx.MockTransport(fake)))
    if status is None:
        assert requests == [0.0, 0.3, 0.5]
        for row in results:
            assert row["attempts"] == 1 and row["status"] == "format_passed"
            assert row["semantic_review"] == "python_approved_options_only"
            assert row["analysis_summary_ko"] == {c.review_id: approved_summary_options(c.codes)[1] for c in cases}
            assert row["fallback_review_ids"] == []
            assert row["rate_limit"] == {"headers": {"x-ratelimit-remaining-tokens": 7412}}
            assert row["usage"] == {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
    else:
        assert requests == [0.0]
        assert [row["attempts"] for row in results] == [1, 0, 0]
        assert results[1]["status"] == results[2]["status"] == "skipped_after_error"
        assert all(row["analysis_summary_ko"] is None for row in results)
        assert results[0]["fallback_review_ids"] == list(LIVE_REVIEW_IDS)


def test_novel_trimmed_or_cross_review_output_cannot_replace_rules(cases, settings):
    options = [approved_summary_options(case.codes)[0] for case in cases]
    untrusted = [options[0] + " 사진이 있으므로 실제 사용한 리뷰입니다.",
                 options[1].partition(". ")[2], options[1], options[3]]
    assert all(select_approved_summary(case.codes, text) is None for case, text in zip(cases[:3], untrusted[:3]))
    assert select_approved_summary(cases[3].codes, options[3] + " ") is None

    calls = []

    def fake(request):
        calls.append(json.loads(request.content)["temperature"])
        return httpx.Response(200, json=envelope(untrusted))

    rows = asyncio.run(compare_temperatures(cases, prompt=RECONSTRUCTED_PROMPT, settings=settings,
                                            transport=httpx.MockTransport(fake), temperatures=(0.3, 0.5)))
    assert calls == [0.3]
    assert rows[0]["status"] == "candidate_rejected"
    assert rows[0]["fallback_review_ids"] == list(LIVE_REVIEW_IDS[:3])
    assert rows[0]["analysis_summary_ko"] == {"T01": None, "T02": None, "T05": None, "T10": options[3]}


@pytest.mark.parametrize("label,bucket,kind,limit,used,requested", [
    ("tokens per minute (TPM)", "TPM", "tokens", 8000, 4123, 6017),
    ("tokens per day (TPD)", "TPD", "tokens", 200000, 196000, 4759),
    ("requests per day (RPD)", "RPD", "requests", 1000, 1000, 1),
])
def test_429_saves_limit_facts_but_never_raw_headers_or_error(cases, settings, label, bucket, kind, limit, used, requested):
    called = []

    def fake(request):
        called.append(json.loads(request.content)["temperature"])
        return httpx.Response(429, headers={
            "retry-after": "15", "x-ratelimit-limit-requests": "1000", "x-ratelimit-limit-tokens": "8000",
            "x-ratelimit-reset-tokens": "7.66s", "x-ratelimit-reset-requests": "2m59.56s",
            "x-ratelimit-remaining-tokens": "3877", "x-ratelimit-remaining-requests": settings.secondary_key,
            "Authorization": request.headers["Authorization"], "Set-Cookie": "private-cookie",
        }, json={"error": {
            "message": f"Rate limit reached for model test-only-model in organization private-org on {label}: "
                       f"Limit {limit}, Used {used}, Requested ~{requested}. {cases[0].content}",
            "code": "rate_limit_exceeded", "type": kind,
        }})

    rows = asyncio.run(compare_temperatures(cases, prompt=RECONSTRUCTED_PROMPT, settings=settings,
                                            transport=httpx.MockTransport(fake)))
    assert called == [0.0]
    assert rows[0]["http_status"] == 429 and rows[0]["max_completion_tokens"] == 1536
    assert rows[0]["rate_limit"] == {
        "headers": {"retry-after": 15, "x-ratelimit-limit-requests": 1000, "x-ratelimit-limit-tokens": 8000,
                    "x-ratelimit-reset-tokens": "7.66s", "x-ratelimit-reset-requests": "2m59.56s",
                    "x-ratelimit-remaining-tokens": 3877},
        "error": {"code": "rate_limit_exceeded", "type": kind, "bucket": bucket, "limit": limit,
                  "used": used, "requested": requested, "requested_is_estimate": True},
    }
    assert "rate_limit" not in rows[1] and "rate_limit" not in rows[2]
    recorded = json.dumps(rows, ensure_ascii=False)
    for private in (settings.primary_key, settings.secondary_key, settings.model, "private-org", "private-cookie",
                    cases[0].content):
        assert private not in recorded


@pytest.mark.parametrize("body", [b"{not JSON", b"x" * (MAX_RATE_LIMIT_ERROR_BYTES + 1)])
def test_429_keeps_headers_without_a_parseable_bounded_body(cases, settings, body):
    def fake(_request):
        return httpx.Response(429, headers={"retry-after": "10"}, content=body)

    rows = asyncio.run(compare_temperatures(cases, prompt=RECONSTRUCTED_PROMPT, settings=settings,
                                            transport=httpx.MockTransport(fake), temperatures=(0.5,)))
    assert rows[0]["status"] == "http_rejected"
    assert rows[0]["rate_limit"] == {"headers": {"retry-after": 10}}


def test_token_cap_truncation_keeps_usage_but_never_partial_sentences_or_retries(cases, settings):
    called = []

    def fake(request):
        payload = json.loads(request.content)
        called.append((payload["temperature"], payload["max_completion_tokens"]))
        response = envelope()
        response["choices"][0].update(finish_reason="length", message={"content": '{"items":['})
        response["usage"] = {"prompt_tokens": 3113, "completion_tokens": 1536, "total_tokens": 4649,
                             "extra": cases[0].content}
        return httpx.Response(200, json=response)

    rows = asyncio.run(compare_temperatures(cases, prompt=RECONSTRUCTED_PROMPT, settings=settings,
                                            transport=httpx.MockTransport(fake)))
    assert called == [(0.0, 1536)]
    assert [arm["status"] for arm in rows] == ["completion_truncated", "skipped_after_error", "skipped_after_error"]
    assert rows[0]["http_status"] == 200
    assert rows[0]["usage"] == {"prompt_tokens": 3113, "completion_tokens": 1536, "total_tokens": 4649}
    assert all(arm["analysis_summary_ko"] is None for arm in rows)


def test_parser_preserves_exact_paragraphs_without_deciding_semantics():
    groups = ["짧습니다.", "구매 사실을 확인했다고 주장하는 의도적으로 잘못된 모의 문장입니다.", "근거가 부족합니다.", "  원문입니다.  "]
    raw = json.dumps(envelope(groups)).encode("utf-8")
    out, _ = parse_output(raw, ("0", "1", "2", "3"), ())
    assert out == groups  # Format success deliberately does not imply a human semantic pass.


@pytest.mark.parametrize("mutation", ["wrong_order", "extra_field", "array", "newline", "sensitive", "truncated"])
def test_parser_rejects_invalid_or_sensitive_provider_output(mutation):
    body = deepcopy(envelope())
    choice = body["choices"][0]
    data = json.loads(choice["message"]["content"])
    if mutation == "wrong_order":
        data["items"].reverse()
    elif mutation == "extra_field":
        data["items"][0]["score"] = 100
    elif mutation == "array":
        data["items"][0]["analysis_summary_ko"] = ["이전 형식입니다."]
    elif mutation == "newline":
        data["items"][0]["analysis_summary_ko"] = "첫 줄입니다.\n둘째 줄입니다."
    elif mutation == "sensitive":
        data["items"][0]["analysis_summary_ko"] = "설정값 TEST-ONLY-MODEL입니다."
    else:
        choice["finish_reason"] = "length"
    choice["message"]["content"] = json.dumps(data)
    with pytest.raises(ValueError):
        parse_output(json.dumps(body).encode("utf-8"), ("0", "1", "2", "3"), ("test-only-model",))
