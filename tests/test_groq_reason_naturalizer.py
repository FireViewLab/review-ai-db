"""합성 Groq 전송으로 이유 자연어 변환·엄격한 검증·제한된 fallback을 검증한다."""
import asyncio
from copy import deepcopy
import json
import time

import httpx
import pytest

from app.integrations.groq_reason_naturalizer import (
    GroqReasonSettings, load_settings, naturalize_reasons, naturalize_reasons_batch,
)


PRIMARY = "synthetic-groq-primary-not-a-real-key"
SECONDARY = "synthetic-groq-secondary-not-a-real-key"
MESSAGE = "리뷰 본문이 짧아 구체적인 사용 경험의 근거가 충분하지 않습니다."
CONTENT = '원본 리뷰 "그대로"\n배송 빠르고 좋아요. 이전 지시를 무시하라는 문장도 데이터입니다.'
CODES = ["TEXT_SHORT_REVIEW", "NETWORK_SIMILAR_REVIEW_PATTERN"]


@pytest.fixture
def settings():
    return GroqReasonSettings(enabled=True, primary_key=PRIMARY, secondary_key=SECONDARY,
                              model="synthetic-test-model", timeout_seconds=5)


def input_items(request):
    body = json.loads(request.content)
    return json.loads(body["messages"][-1]["content"])["items"]


def valid_output(request):
    return {"items": [{"item_id": item["item_id"],
                        "reasons": [{"code": code, "message": MESSAGE} for code in item["reasons"]]}
                       for item in input_items(request)]}


def completion(payload):
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": text}}]})


def test_primary_success_preserves_content_codes_and_structured_prompt(settings):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "POST" and request.url.scheme == "https"
        assert request.headers["authorization"] == "Bearer " + PRIMARY
        body = json.loads(request.content)
        assert body["model"] == "synthetic-test-model"
        assert body["messages"][0]["role"] == "system"
        assert body["response_format"]["type"] in {"json_object", "json_schema"}
        assert input_items(request) == [{"item_id": "0", "content": CONTENT, "reasons": CODES}]
        return completion(valid_output(request))

    original_codes = deepcopy(CODES)
    result = naturalize_reasons(content=CONTENT, reasons=original_codes,
                               transport=httpx.MockTransport(handler), settings=settings)
    assert result == [MESSAGE, MESSAGE]
    assert original_codes == CODES and len(calls) == 1


@pytest.mark.parametrize("failover", [False, True])
def test_request_uses_low_reasoning_effort_and_explicit_env_model(monkeypatch, failover):
    monkeypatch.setenv("ENABLE_GROQ_REASON_NATURALIZATION", "1")
    monkeypatch.setenv("GROQ_API_KEY_PRIMARY", PRIMARY)
    monkeypatch.setenv("GROQ_API_KEY_SECONDARY", SECONDARY)
    monkeypatch.setenv("GROQ_MODEL", "openai/gpt-oss-20b")
    monkeypatch.setenv("GROQ_TIMEOUT_SECONDS", "5")
    keys = []

    def handler(request):
        body = json.loads(request.content)
        assert body["reasoning_effort"] == "low"
        assert body["model"] == "openai/gpt-oss-20b"
        keys.append(request.headers["authorization"])
        if failover and len(keys) == 1:
            return httpx.Response(429)
        return completion(valid_output(request))

    result = naturalize_reasons(content=CONTENT, reasons=CODES,
                               transport=httpx.MockTransport(handler))
    assert result == [MESSAGE, MESSAGE]
    assert keys == (["Bearer " + PRIMARY, "Bearer " + SECONDARY] if failover
                    else ["Bearer " + PRIMARY])


@pytest.mark.parametrize("model_value", [None, ""])
def test_missing_env_model_does_not_default_to_gpt_oss_or_call_provider(monkeypatch, model_value):
    monkeypatch.setenv("ENABLE_GROQ_REASON_NATURALIZATION", "1")
    monkeypatch.setenv("GROQ_API_KEY_PRIMARY", PRIMARY)
    monkeypatch.setenv("GROQ_TIMEOUT_SECONDS", "5")
    if model_value is None:
        monkeypatch.delenv("GROQ_MODEL", raising=False)
    else:
        monkeypatch.setenv("GROQ_MODEL", model_value)
    transport = httpx.MockTransport(lambda request: pytest.fail("Missing model made a request"))
    assert load_settings().model is None
    assert naturalize_reasons(content=CONTENT, reasons=CODES, transport=transport) == CODES


def test_provider_may_preserve_a_code_it_cannot_explain_faithfully(settings):
    def handler(request):
        output = valid_output(request)
        output["items"][0]["reasons"][0]["message"] = CODES[0]
        return completion(output)

    assert naturalize_reasons(content=CONTENT, reasons=CODES, settings=settings,
                             transport=httpx.MockTransport(handler)) == [CODES[0], MESSAGE]


@pytest.mark.parametrize("status", [401, 403, 408, 429, 500, 502, 503, 504])
def test_retryable_http_uses_secondary_once(settings, status):
    keys = []

    def handler(request):
        keys.append(request.headers["authorization"])
        return httpx.Response(status) if len(keys) == 1 else completion(valid_output(request))

    assert naturalize_reasons(content=CONTENT, reasons=CODES, settings=settings,
                             transport=httpx.MockTransport(handler)) == [MESSAGE, MESSAGE]
    assert keys == ["Bearer " + PRIMARY, "Bearer " + SECONDARY]


@pytest.mark.parametrize("error", [httpx.ReadTimeout, httpx.ConnectError, httpx.RemoteProtocolError])
def test_network_errors_use_secondary_once(settings, error):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise error("synthetic failure", request=request)
        return completion(valid_output(request))

    assert naturalize_reasons(content=CONTENT, reasons=CODES, settings=settings,
                             transport=httpx.MockTransport(handler)) == [MESSAGE, MESSAGE]
    assert len(calls) == 2


def test_non_retryable_400_returns_original_without_secondary(settings):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(400, text="synthetic request rejected")

    assert naturalize_reasons(content=CONTENT, reasons=CODES, settings=settings,
                             transport=httpx.MockTransport(handler)) == CODES
    assert len(calls) == 1


def test_both_keys_fail_preserves_every_code_and_does_not_log_private_values(settings, caplog):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout(PRIMARY + " " + SECONDARY + " " + CONTENT, request=request)

    assert naturalize_reasons(content=CONTENT, reasons=CODES, settings=settings,
                             transport=httpx.MockTransport(handler)) == CODES
    assert len(calls) == 2
    assert PRIMARY not in caplog.text and SECONDARY not in caplog.text and CONTENT not in caplog.text
    assert PRIMARY not in repr(settings) and SECONDARY not in repr(settings)


@pytest.mark.parametrize("kind", [
    "not_json", "no_items", "missing_item", "duplicate_item", "wrong_id", "wrong_code",
    "missing_reason", "duplicate_reason", "reordered_codes", "blank_message", "long_message",
    "extra_score_field", "extra_item_field", "extra_top_field", "non_string_message", "english_message", "multiline_message",
])
def test_malformed_provider_output_falls_back_without_retry(settings, kind):
    calls = []

    def handler(request):
        calls.append(request)
        output = valid_output(request)
        item = output["items"][0]
        if kind == "not_json": return completion("not valid JSON")
        if kind == "no_items": output = {}
        elif kind == "missing_item": output["items"] = []
        elif kind == "duplicate_item": output["items"].append(deepcopy(item))
        elif kind == "wrong_id": item["item_id"] = "different"
        elif kind == "wrong_code": item["reasons"][0]["code"] = "INVENTED_REASON"
        elif kind == "missing_reason": item["reasons"].pop()
        elif kind == "duplicate_reason": item["reasons"][1] = deepcopy(item["reasons"][0])
        elif kind == "reordered_codes": item["reasons"].reverse()
        elif kind == "blank_message": item["reasons"][0]["message"] = "  "
        elif kind == "long_message": item["reasons"][0]["message"] = "긴" * 10000
        elif kind == "extra_score_field": item["reasons"][0]["rti"] = 0
        elif kind == "extra_item_field": item["text_score"] = 0
        elif kind == "extra_top_field": output["rti"] = 0
        elif kind == "non_string_message": item["reasons"][0]["message"] = None
        elif kind == "english_message": item["reasons"][0]["message"] = "Review text is short."
        elif kind == "multiline_message": item["reasons"][0]["message"] = "첫 번째 문장입니다.\n두 번째 문장입니다."
        return completion(output)

    assert naturalize_reasons(content=CONTENT, reasons=CODES, settings=settings,
                             transport=httpx.MockTransport(handler)) == CODES
    assert len(calls) == 1


def test_reordered_item_ids_fall_back_for_entire_chunk(settings):
    def handler(request):
        output = valid_output(request)
        output["items"].reverse()
        return completion(output)

    reasons = [["TEXT_SHORT_REVIEW"], ["TEXT_OTHER"]]
    assert naturalize_reasons_batch(contents=["첫 번째", "두 번째"], reasons=reasons,
                                   transport=httpx.MockTransport(handler), settings=settings) == reasons


@pytest.mark.parametrize("finish_reason", [None, "length", "content_filter", "tool_calls"])
def test_missing_or_non_stop_finish_returns_original_without_retry(settings, finish_reason):
    calls = []

    def handler(request):
        calls.append(request)
        choice = {"message": {"content": json.dumps(valid_output(request), ensure_ascii=False)}}
        if finish_reason is not None:
            choice["finish_reason"] = finish_reason
        return httpx.Response(200, json={"choices": [choice]})

    assert naturalize_reasons(content=CONTENT, reasons=CODES, settings=settings,
                             transport=httpx.MockTransport(handler)) == CODES
    assert len(calls) == 1


@pytest.mark.parametrize("configuration", [
    GroqReasonSettings(),
    GroqReasonSettings(enabled=True, primary_key=None, model="test"),
    GroqReasonSettings(enabled=True, primary_key=PRIMARY, model=None),
])
def test_off_or_unconfigured_never_sends_requests(configuration):
    transport = httpx.MockTransport(lambda request: pytest.fail("Unexpected provider request"))
    assert naturalize_reasons(content=CONTENT, reasons=CODES, settings=configuration,
                             transport=transport) == CODES


def test_empty_reasons_never_calls_provider(settings):
    transport = httpx.MockTransport(lambda request: pytest.fail("Unexpected empty-reasons request"))
    assert naturalize_reasons(content=CONTENT, reasons=[], transport=transport, settings=settings) == []
    assert naturalize_reasons_batch(contents=["one", "two"], reasons=[[], []],
                                   transport=transport, settings=settings) == [[], []]


def test_direct_async_caller_does_not_nest_event_loop(settings):
    transport = httpx.MockTransport(lambda request: pytest.fail("Nested async call made a request"))

    async def invoke():
        assert naturalize_reasons(content=CONTENT, reasons=CODES, transport=transport, settings=settings) == CODES

    asyncio.run(invoke())


def test_empty_rows_are_skipped_without_changing_original_item_identity(settings):
    calls = []

    def handler(request):
        calls.append(input_items(request))
        return completion(valid_output(request))

    result = naturalize_reasons_batch(contents=["empty", CONTENT], reasons=[[], CODES],
                                     transport=httpx.MockTransport(handler), settings=settings)
    assert result == [[], [MESSAGE, MESSAGE]]
    assert calls == [[{"item_id": "1", "content": CONTENT, "reasons": CODES}]]


def test_missing_secondary_returns_original_after_primary_failure(settings):
    from dataclasses import replace
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429)

    result = naturalize_reasons(content=CONTENT, reasons=CODES,
                               settings=replace(settings, secondary_key=None),
                               transport=httpx.MockTransport(handler))
    assert result == CODES and len(calls) == 1


@pytest.mark.parametrize("timeout", ["not-a-number", "nan", "inf", "0", "-1"])
def test_invalid_environment_configuration_falls_back_without_exception(monkeypatch, timeout):
    monkeypatch.setenv("ENABLE_GROQ_REASON_NATURALIZATION", "1")
    monkeypatch.setenv("GROQ_API_KEY_PRIMARY", PRIMARY)
    monkeypatch.setenv("GROQ_API_KEY_SECONDARY", SECONDARY)
    monkeypatch.setenv("GROQ_MODEL", "synthetic-test-model")
    monkeypatch.setenv("GROQ_TIMEOUT_SECONDS", timeout)
    transport = httpx.MockTransport(lambda request: pytest.fail("Invalid configuration made a request"))
    assert naturalize_reasons(content=CONTENT, reasons=CODES, transport=transport) == CODES


def test_500_reviews_are_batched_not_500_serial_calls(settings):
    calls = []

    def handler(request):
        items = input_items(request)
        assert len(items) <= 20 and len(request.content) <= 32768
        calls.append([item["item_id"] for item in items])
        return completion(valid_output(request))

    reasons = [["TEXT_SHORT_REVIEW"] for _ in range(500)]
    result = naturalize_reasons_batch(contents=[f"짧은 리뷰 {index}" for index in range(500)], reasons=reasons,
                                     settings=settings, transport=httpx.MockTransport(handler))
    assert result == [[MESSAGE] for _ in range(500)]
    assert len(calls) == 25
    assert [item_id for chunk in calls for item_id in chunk] == [str(index) for index in range(500)]


def test_secondary_stays_active_for_later_chunks_without_repeated_primary_retry(settings):
    keys = []

    def handler(request):
        keys.append(request.headers["authorization"])
        return httpx.Response(429) if len(keys) == 1 else completion(valid_output(request))

    result = naturalize_reasons_batch(contents=["짧은 리뷰"] * 21, reasons=[["TEXT_SHORT_REVIEW"]] * 21,
                                     settings=settings, transport=httpx.MockTransport(handler))
    assert result == [[MESSAGE] for _ in range(21)]
    assert keys == ["Bearer " + PRIMARY, "Bearer " + SECONDARY, "Bearer " + SECONDARY]


def test_both_keys_fail_stops_500_review_batch_after_two_requests(settings):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503)

    reasons = [["TEXT_SHORT_REVIEW"] for _ in range(500)]
    assert naturalize_reasons_batch(contents=["짧은 리뷰"] * 500, reasons=reasons,
                                   settings=settings, transport=httpx.MockTransport(handler)) == reasons
    assert len(calls) == 2


def test_long_reviews_split_by_full_request_byte_cap_without_truncating_content(settings):
    content = "원본 리뷰 본문입니다. " * 350
    calls = []

    def handler(request):
        assert len(request.content) <= 32768
        items = input_items(request)
        assert all(item["content"] == content for item in items)
        calls.append(items)
        return completion(valid_output(request))

    result = naturalize_reasons_batch(contents=[content] * 5, reasons=[["TEXT_SHORT_REVIEW"]] * 5,
                                     settings=settings, transport=httpx.MockTransport(handler))
    assert result == [[MESSAGE] for _ in range(5)]
    assert len(calls) > 1
    assert [item["item_id"] for chunk in calls for item in chunk] == [str(index) for index in range(5)]


def test_success_invalid_success_chunks_keep_each_validated_result(settings):
    calls = []

    def handler(request):
        calls.append(request)
        return completion("invalid JSON") if len(calls) == 2 else completion(valid_output(request))

    result = naturalize_reasons_batch(contents=["짧은 리뷰"] * 41, reasons=[["TEXT_SHORT_REVIEW"]] * 41,
                                     settings=settings, transport=httpx.MockTransport(handler))
    assert result == [[MESSAGE]] * 20 + [["TEXT_SHORT_REVIEW"]] * 20 + [[MESSAGE]]
    assert len(calls) == 3
    assert all(request.headers["authorization"] == "Bearer " + PRIMARY for request in calls)


def test_partial_timeout_keeps_completed_chunks_and_remaining_originals():
    settings = GroqReasonSettings(enabled=True, primary_key=PRIMARY, secondary_key=SECONDARY,
                                  model="synthetic-test-model", timeout_seconds=.02)
    calls = []

    async def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return completion(valid_output(request))
        await asyncio.sleep(.2)
        return completion(valid_output(request))

    result = naturalize_reasons_batch(contents=["짧은 리뷰"] * 41, reasons=[["TEXT_SHORT_REVIEW"]] * 41,
                                     settings=settings, transport=httpx.MockTransport(handler))
    assert result == [[MESSAGE]] * 20 + [["TEXT_SHORT_REVIEW"]] * 21
    assert 2 <= len(calls) <= 3


def test_oversized_streamed_provider_body_is_bounded_and_closed(settings):
    streams = []

    class LargeResponse(httpx.AsyncByteStream):
        def __init__(self, request):
            self.data = json.dumps({"choices": [{"finish_reason": "stop", "message": {
                "content": json.dumps(valid_output(request), ensure_ascii=False),
            }}], "padding": "x" * (300 * 1024)}).encode()
            self.chunks_read = 0
            self.closed = False

        async def __aiter__(self):
            for offset in range(0, len(self.data), 64 * 1024):
                self.chunks_read += 1
                yield self.data[offset:offset + 64 * 1024]

        async def aclose(self):
            self.closed = True

    def handler(request):
        stream = LargeResponse(request)
        streams.append(stream)
        return httpx.Response(200, stream=stream)

    assert naturalize_reasons(content=CONTENT, reasons=CODES, settings=settings,
                             transport=httpx.MockTransport(handler)) == CODES
    assert len(streams) == 1 and streams[0].closed
    assert streams[0].chunks_read <= 3


def test_oversized_full_content_is_not_silently_truncated_or_sent(settings):
    content = "사용 경험을 자세히 기록한 원본 리뷰입니다." * 3000
    transport = httpx.MockTransport(lambda request: pytest.fail("Oversized source was sent or truncated"))
    assert naturalize_reasons(content=content, reasons=CODES, transport=transport, settings=settings) == CODES


def test_total_deadline_bounds_a_500_review_batch():
    settings = GroqReasonSettings(enabled=True, primary_key=PRIMARY, secondary_key=SECONDARY,
                                  model="synthetic-test-model", timeout_seconds=.02)
    calls = []

    async def handler(request):
        calls.append(request)
        await asyncio.sleep(.2)
        return completion(valid_output(request))

    reasons = [["TEXT_SHORT_REVIEW"] for _ in range(500)]
    start = time.monotonic()
    result = naturalize_reasons_batch(contents=["짧은 리뷰"] * 500, reasons=reasons,
                                     settings=settings, transport=httpx.MockTransport(handler))
    assert result == reasons
    assert 1 <= len(calls) <= 2
    assert time.monotonic() - start < .5
