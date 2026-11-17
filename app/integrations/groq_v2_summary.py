"""V2-only, bounded Groq enrichment. No review text, identifiers, or scores leave this layer."""

import asyncio
import json
import logging
import math
import os
from dataclasses import dataclass, field
from threading import Lock
from time import monotonic

import httpx

from app.integrations.groq_reason_naturalizer import _read_response

MODEL = "openai/gpt-oss-20b"
MAX_BATCH = 12
INTERVAL_SECONDS = 60
MAX_COMPLETION_TOKENS = 1536
CONNECTIVES = ("또한 ", "참고로 ", "한편 ", "이와 별도로 ")
PROMPT = (
    "각 항목의 한국어 정책 문장을 자연스러운 한 문단으로 연결하세요. facts_ko 문장은 글자 단위로 원문 순서 그대로 "
    "사용하고, 문장 사이를 공백으로 연결하세요. 첫 문장 이후 원한다면 또한, 참고로, 한편, 이와 별도로 중 어울리는 "
    "연결어를 추가할 수 있습니다. 의미를 바꾸거나 판단·사실·문장을 추가하지 마세요. 구매나 실제 사용을 추정하지 말고, "
    "현황 미확인을 0장이나 낮은 별점으로 바꾸지 마세요. 원래 순서와 item_id를 유지해 JSON으로만 반환하세요."
)
LOGGER = logging.getLogger(__name__)


def facts(paragraph: str) -> list[str]:
    return [part + "." for part in paragraph.removesuffix(".").split(". ")]


def verified_paragraph(base: str, candidate: object) -> str | None:
    """Allow only non-factual connectives between exact, immutable Python sentences."""
    if not isinstance(candidate, str) or len(candidate) > 1000 or "\n" in candidate or "\r" in candidate:
        return None
    expected, actual = facts(base), facts(candidate)
    if not candidate.endswith(".") or len(expected) != len(actual) or expected[0] != actual[0]:
        return None
    for original, rendered in zip(expected[1:], actual[1:], strict=True):
        if rendered != original and not any(rendered == prefix + original for prefix in CONNECTIVES):
            return None
    return candidate


def request_payload(paragraphs: list[str]) -> dict:
    items = [{"item_id": str(index), "facts_ko": facts(text)} for index, text in enumerate(paragraphs)]
    item = {"type": "object", "additionalProperties": False, "required": ["item_id", "analysis_summary_ko"],
            "properties": {"item_id": {"type": "string", "enum": [obj["item_id"] for obj in items]},
                           "analysis_summary_ko": {"type": "string"}}}
    schema = {"type": "object", "additionalProperties": False, "required": ["items"],
              "properties": {"items": {"type": "array", "items": item}}}
    return {"model": MODEL, "temperature": 0.3, "top_p": 1.0, "reasoning_effort": "low",
            "max_completion_tokens": MAX_COMPLETION_TOKENS,
            "messages": [{"role": "system", "content": PROMPT},
                         {"role": "user", "content": json.dumps({"items": items}, ensure_ascii=False)}],
            "response_format": {"type": "json_schema", "json_schema": {
                "name": "review_v2_policy_summary", "strict": True, "schema": schema}}}


@dataclass
class SummaryOutcome:
    status: str
    paragraphs: dict[int, str] = field(default_factory=dict)
    usage: dict = field(default_factory=dict)
    candidates: dict[int, str] = field(default_factory=dict)
    http_status: int | None = None
    seconds: float | None = None
    retry_after: int | None = None
    limits: dict = field(default_factory=dict)


def parse_completion(raw: bytes, paragraphs: list[str], *, audit: bool, blocked: str) -> SummaryOutcome:
    body = json.loads(raw)
    usage = body.get("usage", {})
    counts = {name: usage[name] for name in ("prompt_tokens", "completion_tokens", "total_tokens")
              if isinstance(usage, dict) and type(usage.get(name)) is int and usage[name] >= 0}
    choices = body["choices"]
    if len(choices) != 1 or choices[0]["finish_reason"] != "stop":
        return SummaryOutcome("incomplete", usage=counts)
    obj = json.loads(choices[0]["message"]["content"])
    items = obj["items"]
    if set(obj) != {"items"} or not isinstance(items, list) or len(items) != len(paragraphs):
        return SummaryOutcome("invalid_output", usage=counts)
    accepted, candidates = {}, {}
    for index, (row, base) in enumerate(zip(items, paragraphs, strict=True)):
        if set(row) != {"item_id", "analysis_summary_ko"} or row["item_id"] != str(index):
            return SummaryOutcome("invalid_output", usage=counts)
        text = row["analysis_summary_ko"]
        if not isinstance(text, str) or len(text) > 1000 or (blocked and blocked in text):
            continue
        if audit:
            candidates[index] = text
        verified = verified_paragraph(base, text)
        if verified is not None and verified != base:
            accepted[index] = verified
    return SummaryOutcome("verified" if accepted else "python_fallback", accepted, counts, candidates)


class V2GroqSummary:
    def __init__(self, *, key: str = "", timeout: float = 5, transport=None):
        self._key = key
        self._timeout = timeout
        self._transport = transport
        self._lock = Lock()
        self._after = 0.0

    @classmethod
    def from_env(cls):
        if os.getenv("ENABLE_V2_GROQ_SUMMARY") != "1":
            return cls()
        try:
            timeout = float(os.getenv("GROQ_TIMEOUT_SECONDS", "5"))
        except ValueError:
            return cls()
        if not math.isfinite(timeout) or not 0 < timeout <= 30 or os.getenv("V2_GROQ_MODEL", MODEL) != MODEL:
            return cls()
        return cls(key=os.getenv("GROQ_API_KEY_PRIMARY", "").strip(), timeout=timeout)

    def enrich(self, paragraphs: list[str], *, audit=False) -> SummaryOutcome:
        if not self._key or not paragraphs:
            return SummaryOutcome("disabled_or_empty")
        if not self._lock.acquire(blocking=False):
            return SummaryOutcome("busy")
        try:
            if monotonic() < self._after:
                return SummaryOutcome("cooldown")
            self._after = monotonic() + INTERVAL_SECONDS
            start = monotonic()
            outcome = asyncio.run(self._call(paragraphs[:MAX_BATCH], audit=audit))
            outcome.seconds = round(monotonic() - start, 6)
            if outcome.http_status == 429:
                self._after = max(self._after, monotonic() + (outcome.retry_after or INTERVAL_SECONDS))
            LOGGER.info("v2 Groq summary status=%s http=%s usage=%s seconds=%s",
                        outcome.status, outcome.http_status, outcome.usage, outcome.seconds)
            return outcome
        finally:
            self._lock.release()

    async def _call(self, paragraphs, *, audit):
        headers = {}

        async def capture(response):
            for name in ("retry-after", "x-ratelimit-remaining-requests", "x-ratelimit-remaining-tokens"):
                value = response.headers.get(name, "")
                if value.isascii() and value.isdigit() and len(value) < 10:
                    headers[name] = int(value)

        try:
            async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=False, trust_env=False,
                                         event_hooks={"response": [capture]}, transport=self._transport) as client:
                status, raw = await asyncio.wait_for(_read_response(client, request_payload(paragraphs), self._key),
                                                     timeout=self._timeout)
            if status != 200 or raw is None:
                return SummaryOutcome("provider_rejected", http_status=status, retry_after=headers.get("retry-after"), limits=headers)
            try:
                outcome = parse_completion(raw, paragraphs, audit=audit, blocked=self._key)
            except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
                outcome = SummaryOutcome("invalid_output")
            outcome.http_status, outcome.limits = status, headers
            return outcome
        except (TimeoutError, httpx.HTTPError):
            return SummaryOutcome("transport_failure")
