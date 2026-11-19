"""Local Groq paragraph comparison for four verified synthetic reviews."""

import argparse
import asyncio
from hashlib import sha256
import json
from pathlib import Path
import re
import sys
from time import perf_counter

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.integrations.groq_reason_naturalizer import (
    MAX_INPUT_BYTES, MAX_MESSAGE_CHARS, _json, _read_response, load_settings,
)
from app.experimental.summary_candidate import SUMMARY_CANDIDATE_VERSION, approved_summary_options, select_approved_summary
from scripts.compare_ai_v3_groq_reasons import LIVE_REVIEW_IDS, LOCAL_OUTPUT, load_cases

TEMPERATURES = (0.0, 0.3, 0.5)
FIRST_TEMPERATURE = 0.3
LIVE_MODEL = "openai/gpt-oss-20b"
FREEFORM_MAX_COMPLETION_TOKENS = 1536  # Local experiment only; not shared with the production request.
MAX_SUMMARY_CHARS = 2 * MAX_MESSAGE_CHARS
MAX_RATE_LIMIT_ERROR_BYTES = 8 * 1024
COUNTER = r"[0-9]{1,12}"
DURATION = r"(?:[0-9]{1,9}(?:\.[0-9]{1,9})?(?:ms|us|µs|ns|s|m|h|d)){1,5}"
RATE_LIMIT_HEADER_FORMATS = {
    "retry-after": COUNTER,
    "x-ratelimit-limit-requests": COUNTER,
    "x-ratelimit-remaining-requests": COUNTER,
    "x-ratelimit-reset-requests": DURATION,
    "x-ratelimit-limit-tokens": COUNTER,
    "x-ratelimit-remaining-tokens": COUNTER,
    "x-ratelimit-reset-tokens": DURATION,
}
RATE_LIMIT_MESSAGE = re.compile(
    r"\bon (?:requests per day \(RPD\)|requests per minute \(RPM\)|"
    r"tokens per minute \(TPM\)|input tokens per minute \(ITPM\)|"
    r"output tokens per minute \(OTPM\)|tokens per day \(TPD\)):"
    rf"\s*Limit (?P<limit>{COUNTER})\b(?!,[0-9])"
    rf"(?:,\s*Used (?P<used>{COUNTER})\b(?!,[0-9]))?"
    rf"(?:,\s*Requested (?P<approx>~?)(?P<requested>{COUNTER})\b(?!,[0-9]))?",
    re.IGNORECASE,
)
RECONSTRUCTED_PROMPT = """Re:view는 리뷰 본문과 분석기에서 관찰한 단서를 사용해 리뷰를 읽는 데 도움을 줍니다.
코드를 낱개로 번역하지 말고, 해당 리뷰를 이해하는 데 중요한 근거부터 연결해 이유를 설명하세요.
적절하다면 짧은 본문 표현을 인용할 수 있지만, 그 표현이 왜 판단에 관련되는지도 설명하세요.
한국어 존댓말로 간결하게 쓰고, 반복이나 모호한 문장을 피하세요."""

GUARDRAILS = """이 모드는 새 문장을 작성하지 않고 Python이 승인한 문단을 선택하는 모드입니다.
위 설명 원칙은 읽기 좋은 후보를 고르는 데만 사용합니다. 판정과 문장은 고치지 마세요."""

SUMMARY_FORMAT = """각 항목의 approved_summaries_ko에서 가장 자연스럽게 읽히는 종합 평가 한 문단을 고르세요.
선택한 문자열을 글자, 문장, 순서, 공백까지 그대로 복사하세요. 새 문장을 만들거나 후보를 섞지 마세요.
items 배열의 각 항목에 입력과 동일한 item_id 및 선택한 analysis_summary_ko 문자열을 담고 순서를 유지하세요.
스키마에 맞는 JSON만 반환하세요."""

# Historical diagnostics for earlier freeform result files. Never grants acceptance
# in the closed-set candidate path below.
POLICY_ANCHORS = {
    "VERIFIED_PHOTO_AND_NO_PROMOTIONAL_STYLE": (
        "확인된 사진과 홍보성 표현 조합 미탐지가 함께 긍정 조건이다. 별점 5점은 이 결합 조건을 대신하지 않는다.",
        ((r"(?:확인된|첨부된)\s*사진|사진[^.!?]{0,24}(?:확인(?:됐|되었|되고|된|됩니다)|첨부|있)",
          r"홍보성\s*표현[^.!?]{0,24}조합[^.!?]{0,24}(?:미탐지|(?:감지|탐지|확인|관찰)되지\s*않)",
          r"함께|조합이|결합|긍정.{0,12}(?:조건|근거|요소|반영)"),),
    ),
    "VERIFIED_NO_PHOTO_ULTRASHORT_FIVE_STAR": (
        "사진 0장·검증된 별점 5점·의미 있는 본문 10자 미만이라는 세 조건이 함께 정책상 주의 근거다.",
        ((r"사진[^.!?]{0,14}(?:0\s*장|없)|(?:0\s*장|없는)\s*사진|사진\s*부재",
          r"(?:검증|확인)[^.!?]{0,16}5\s*점|5\s*점[^.!?]{0,16}(?:확인|검증)",
          r"의미\s*있는[^.!?]{0,18}10\s*자\s*미만|10\s*자\s*미만[^.!?]{0,14}의미\s*있는",
          r"함께|결합|겹치|맞물|(?:세|3)\s*(?:가지\s*)?조건|조합"),),
    ),
    "PHOTO_EVIDENCE_UNKNOWN": (
        "사진 개수와 별점은 미확인이다. 미확인 사진 조건은 판정에 사용하지 않았다. 사진이 0장이거나 위험하다는 뜻이 아니다.",
        ((r"사진", r"별점|평점", r"미확인|확인(?:할\s*수\s*없|되지\s*않|하[지기]\s*(?:못|어렵))|알\s*수\s*없"),
         (r"사진", r"판정|평가|정책", r"(?:사용|반영|적용|활용|포함)(?:하지\s*않|되지\s*않|하지\s*못)|제외")),
    ),
    "SAFE_REQUIREMENTS_INCOMPLETE": (
        "긍정 구간에 필요한 정책 조건이 모두 충족됐다고 확인할 수 없다. 제품 안전이나 리뷰의 진위 판단이 아니다.",
        ((r"긍정", r"조건|기준", r"미충족|미확인|어렵|충족[^.!?]{0,18}(?:않|못)|확인[^.!?]{0,12}(?:않|못|없)"),),
    ),
    "MEANINGFUL_TEXT_10_29_GENERAL_PENALTY": (
        "의미 있는 본문이 10~29자 구간에 해당해 길이 감점이 반영됐다.",
        ((r"의미\s*있는", r"10\s*[~∼–—-]\s*29\s*자|10\s*자\s*이상[^.!?]{0,10}29\s*자\s*이하", r"(?:길이|분량)[^.!?]{0,16}감점"),),
    ),
}
UNSUPPORTED_INFERENCES = {
    "photo_implies_purchase_or_use": (
        r"사진[^.!?]{0,50}(?:실제\s*)?(?:구매|사용)[^.!?]{0,35}"
        r"(?:시사(?:합|하|할|한)|입증(?:합|하|할|한)|증명(?:합|하|할|한)|보여\s*줍)"
    ),
    "undocumented_experience_treated_as_absent": (
        r"사용\s*경험(?:이|은)\s*(?:없|부족)"
    ),
    "promotional_pattern_miss_treated_as_absence": r"홍보성\s*표현(?:이|은)\s*(?:전혀\s*)?없",
}


def check_evidence(summary: str, codes: tuple[str, ...]) -> dict:
    sentences = re.split(r"[.!?。！？](?=\s|$)", summary)
    required = [code for code in codes if code in POLICY_ANCHORS]
    missing = [code for code in required if not all(
        any(all(re.search(pattern, sentence) for pattern in group) for sentence in sentences)
        for group in POLICY_ANCHORS[code][1]
    )]
    flags = [name for name, pattern in UNSUPPORTED_INFERENCES.items() if re.search(pattern, summary)]
    return {"status": "needs_fallback" if missing or flags or not required else "human_review_required",
            "required_codes": required, "unverified_codes": missing, "inference_flags": flags,
            "display_source": "rules_reasons_ko"}


def request_items(cases) -> list[dict]:
    if tuple(case.review_id for case in cases) != LIVE_REVIEW_IDS:
        raise ValueError("Only the four approved synthetic case IDs are allowed")
    items = []
    for index, case in enumerate(cases):
        options = approved_summary_options(case.codes)
        if not options:
            raise ValueError("Unsupported evidence profile; keep rule-based reasons")
        # No review text, score, level, local review ID, or raw codes are needed to select wording.
        items.append({"item_id": str(index), "approved_summaries_ko": list(options)})
    return items


def output_schema(items: list[dict]) -> dict:
    return {
        "type": "object", "required": ["items"], "additionalProperties": False,
        "properties": {"items": {"type": "array", "items": {
            "type": "object", "required": ["item_id", "analysis_summary_ko"],
            "additionalProperties": False,
            "properties": {
                "item_id": {"type": "string", "enum": [item["item_id"] for item in items]},
                "analysis_summary_ko": {"type": "string"},
            },
        }}},
    }


def system_prompt(prompt: str) -> str:
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("Empty source prompt")
    before, heading, old_format = prompt.strip().partition("\n## 7. 출력 형식\n")
    if heading:
        _, check, tail = old_format.partition("최종 작성 전에 다음을 확인하세요.")
        if not check:
            raise ValueError("Source output section is missing its closing checklist")
        prompt = f"{before}{heading}\n{SUMMARY_FORMAT}\n\n{check}{tail}"
    else:
        prompt = f"{before}\n\n{SUMMARY_FORMAT}"
    return f"{prompt}\n\n{GUARDRAILS}"


def payloads(cases, *, prompt: str, model: str) -> list[dict]:
    items = request_items(cases)
    common = {
        "model": model,
        "top_p": 1.0,
        "reasoning_effort": "low",
        "max_completion_tokens": FREEFORM_MAX_COMPLETION_TOKENS,
        "messages": [
            {"role": "system", "content": system_prompt(prompt)},
            {"role": "user", "content": _json({"items": items})},
        ],
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "review_evidence_summary_ko", "strict": True, "schema": output_schema(items),
        }},
    }
    requests = [dict(common, temperature=t) for t in TEMPERATURES]
    if any(len(_json(req).encode("utf-8")) > MAX_INPUT_BYTES for req in requests):
        raise ValueError("Test payload exceeds the existing client byte limit")
    return requests


class CompletionTruncated(ValueError):
    def __init__(self, usage: dict):
        super().__init__("Completion reached its token limit")
        self.usage = usage


def _usage_counters(usage) -> dict:
    usage = usage if isinstance(usage, dict) else {}
    return {key: usage[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens")
            if type(usage.get(key)) is int and usage[key] >= 0}


def parse_output(raw: bytes, expected_ids: tuple[str, ...], blocked: tuple[str, ...]) -> tuple[list[str], dict]:
    envelope = json.loads(raw)
    choices = envelope["choices"]
    if len(choices) != 1:
        raise ValueError("Completion is incomplete or ambiguous")
    finish_reason = choices[0]["finish_reason"]
    usage = _usage_counters(envelope.get("usage"))
    if finish_reason == "length":
        raise CompletionTruncated(usage)
    if finish_reason != "stop":
        raise ValueError("Completion is incomplete or ambiguous")
    response = json.loads(choices[0]["message"]["content"])
    if not isinstance(response, dict) or set(response) != {"items"}:
        raise ValueError("Wrong envelope")
    items = response["items"]
    if not isinstance(items, list) or len(items) != len(expected_ids):
        raise ValueError("Wrong item count")
    result = []
    for item, expected in zip(items, expected_ids, strict=True):
        if not isinstance(item, dict) or set(item) != {"item_id", "analysis_summary_ko"} or item["item_id"] != expected:
            raise ValueError("Wrong item or order")
        msg = item["analysis_summary_ko"]
        if (not isinstance(msg, str) or not msg.strip() or len(msg) > MAX_SUMMARY_CHARS
                or "\r" in msg or "\n" in msg or not any("가" <= char <= "힣" for char in msg)
                or any(value in msg.casefold() for value in blocked)):
            raise ValueError("Invalid or sensitive explanation")
        result.append(msg)  # Preserve the exact returned paragraph for human review.
    return result, usage


async def _capture_rate_limit(response: httpx.Response, details: dict) -> None:
    """Keep documented 200/429 limit headers and structured facts from a bounded 429 body."""
    if response.status_code not in (200, 429):
        return
    details["headers"] = {
        name: int(value) if pattern == COUNTER else value
        for name, pattern in RATE_LIMIT_HEADER_FORMATS.items()
        if (value := response.headers.get(name)) is not None and re.fullmatch(pattern, value)
    }
    if response.status_code != 429:
        return
    raw = bytearray()
    try:
        async for chunk in response.aiter_bytes():
            if len(raw) + len(chunk) > MAX_RATE_LIMIT_ERROR_BYTES:
                return
            raw.extend(chunk)
        envelope = json.loads(raw)
    except (httpx.HTTPError, ValueError, RecursionError):
        return  # Header diagnostics survive a missing, malformed, or unreadable error body.
    error = envelope.get("error") if isinstance(envelope, dict) else None
    if not isinstance(error, dict):
        return
    facts = {}
    if error.get("code") == "rate_limit_exceeded":
        facts["code"] = "rate_limit_exceeded"
    if (error_type := error.get("type")) in ("requests", "tokens", "rate_limit_error"):
        facts["type"] = error_type
    message = error.get("message")
    if isinstance(message, str) and (match := RATE_LIMIT_MESSAGE.search(message)):
        # Never persist the provider's raw text, which may contain an org ID or echoed data.
        facts["bucket"] = match[0].split("(", 1)[1].split(")", 1)[0].upper()
        for metric in ("limit", "used", "requested"):
            if value := match[metric]:
                facts[metric] = int(value)
        if match["approx"]:
            facts["requested_is_estimate"] = True
    if facts:
        details["error"] = facts


async def compare_temperatures(cases, *, prompt, settings, transport=None, temperatures=TEMPERATURES) -> list[dict]:
    """One request per arm, existing bounded transport, no retry, stop after an error."""
    if not temperatures or tuple(t for t in TEMPERATURES if t in temperatures) != tuple(temperatures):
        raise ValueError("Unexpected or repeated temperature")
    requests = [request for request in payloads(cases, prompt=prompt, model=settings.model)
                if request["temperature"] in temperatures]
    blocked = tuple(value.casefold() for value in
                    (settings.primary_key, settings.secondary_key, settings.model) if value)
    if any(value in req_msg["content"].casefold() for value in blocked for req_msg in requests[0]["messages"]):
        raise ValueError("Prompt or synthetic data contains a configured secret or model identifier")
    expected_ids = tuple(str(idx) for idx in range(len(cases)))
    results = []
    rate_limit = {}
    response_status = None

    async def capture(response):
        nonlocal response_status
        response_status = response.status_code
        await _capture_rate_limit(response, rate_limit)

    async with httpx.AsyncClient(timeout=settings.timeout_seconds, trust_env=False, follow_redirects=False,
                                transport=transport, event_hooks={"response": [capture]}) as client:
        for request in requests:
            arm = {"temperature": request["temperature"], "attempts": 0, "http_status": None,
                   "status": "skipped_after_error", "wall_seconds": None, "usage": {},
                   "analysis_summary_ko": None, "semantic_review": "not_performed",
                   "fallback_review_ids": [case.review_id for case in cases]}
            if not results or results[-1]["status"] == "format_passed":
                arm.update(attempts=1, max_completion_tokens=request["max_completion_tokens"])
                rate_limit.clear()
                response_status = None
                start = perf_counter()
                try:
                    status, raw = await asyncio.wait_for(
                        _read_response(client, request, settings.primary_key), timeout=settings.timeout_seconds,
                    )
                    arm.update(http_status=status, status="http_rejected" if status != 200 else "invalid_output")
                    if status == 200 and raw is not None:
                        summaries, usage = parse_output(raw, expected_ids, blocked)
                        selected = {case.review_id: select_approved_summary(case.codes, text)
                                    for case, text in zip(cases, summaries, strict=True)}
                        rejected = [review_id for review_id, text in selected.items() if text is None]
                        arm.update(status="candidate_rejected" if rejected else "format_passed", usage=usage,
                                   semantic_review="rejected" if rejected else "python_approved_options_only",
                                   analysis_summary_ko=selected, fallback_review_ids=rejected)
                except (TimeoutError, httpx.TransportError):
                    arm["status"] = "timeout_or_transport_error"
                except CompletionTruncated as exc:
                    arm.update(status="completion_truncated", usage=exc.usage)
                except (KeyError, TypeError, ValueError, AttributeError, IndexError):
                    arm["status"] = "invalid_output"
                if response_status == 429:
                    arm.update(http_status=429, status="http_rejected")
                if rate_limit:
                    arm["rate_limit"] = dict(rate_limit)
                arm["wall_seconds"] = round(perf_counter() - start, 6)
            results.append(arm)
    return results


def comparison_rows(cases, source_path: Path, previous_path: Path) -> list[dict]:
    source = json.loads(source_path.read_text(encoding="utf-8"))["comparison"]["rows"]
    source = {row["review_id"]: row for row in source}
    previous = json.loads(previous_path.read_text(encoding="utf-8"))
    if (previous["mode"] != "live" or previous["request_statuses"] != [200]
            or previous["schema_and_options_accepted"] is not True
            or [row["review_id"] for row in previous["rows"]] != list(LIVE_REVIEW_IDS)):
        raise ValueError("The original four successful 1:1 responses are required")
    rows = []
    for case, baseline in zip(cases, previous["rows"], strict=True):
        if (baseline["policy_index_v3"] != case.policy_index_v3
                or baseline["evidence_level_v3"] != case.evidence_level_v3
                or baseline["evidence_reasons_v3"] != list(case.codes)
                or baseline["rules_reasons_ko"] != list(case.templates)
                or not isinstance(baseline["groq_reasons_ko"], list)
                or len(baseline["groq_reasons_ko"]) != len(case.codes)):
            raise ValueError("Prior 1:1 comparison and policy versions do not match")
        rows.append({"review_id": case.review_id, "policy_index_v3": case.policy_index_v3,
                     "evidence_level_v3": case.evidence_level_v3,
                     "original_rti": source[case.review_id]["original_rti"],
                     "original_level": source[case.review_id]["original_level"],
                     "evidence_reasons_v3": list(case.codes), "rules_reasons_ko": list(case.templates),
                     "display_reasons_ko": list(case.templates), "prior_1_to_1_ko": baseline["groq_reasons_ko"]})
    return rows


def resume_arms(previous: dict, *, prompt_digest: str, prompt_source: str, baseline: list[dict]) -> dict:
    """Preserve every charged attempt, even rejected or timed-out requests."""
    if (previous["experiment"] != "local_only" or previous.get("summary_candidate_version") != SUMMARY_CANDIDATE_VERSION
            or previous["effective_prompt_sha256"] != prompt_digest
            or previous["source_prompt"] != prompt_source or previous["rows"] != baseline
            or previous["top_p"] != 1.0 or [arm["temperature"] for arm in previous["arms"]] != list(TEMPERATURES)):
        raise ValueError("Previous experiment does not match the approved comparison")
    if previous.get("max_completion_tokens") != FREEFORM_MAX_COMPLETION_TOKENS:
        raise ValueError("Previous completion cap differs or is unrecorded")
    attempted = {}
    for arm in previous["arms"]:
        n = arm["attempts"]
        if type(n) is not int or n not in (0, 1):
            raise ValueError("Invalid attempt count")
        if n:
            if arm.get("max_completion_tokens") != FREEFORM_MAX_COMPLETION_TOKENS:
                raise ValueError("Attempted arm completion cap differs or is unrecorded")
            if arm["status"] != "format_passed" or arm["http_status"] != 200 or not arm.get("analysis_summary_ko"):
                raise ValueError("Stop after an unsuccessful request")
            attempted[arm["temperature"]] = arm
        elif arm["status"] != "not_requested" or arm["http_status"] is not None:
            raise ValueError("Unclear whether the previous arm consumed a request")
    if (FIRST_TEMPERATURE not in attempted or len(attempted) == len(TEMPERATURES)
            or previous["actual_requests"] != len(attempted)):
        raise ValueError("No valid pending experiment arms")
    return attempted


def offline_preview(cases, baseline: list[dict]) -> dict:
    """Return Python-built previews without loading dotenv or initializing HTTP."""
    rows = []
    for case, row in zip(cases, baseline, strict=True):
        options = approved_summary_options(case.codes)
        rows.append({**row, "analysis_summary_ko": options[0] if options else None,
                     "approved_summaries_ko": list(options), "display_source": "rules_reasons_ko"})
    return {"experiment": "offline_preview", "summary_candidate_version": SUMMARY_CANDIDATE_VERSION,
            "model": LIVE_MODEL, "temperature": FIRST_TEMPERATURE, "actual_requests": 0, "rows": rows}


def _write_result(output: Path, data: dict) -> None:
    with output.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--comparison", type=Path, required=True)
    parser.add_argument("--prior-live", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume-from", type=Path, help="Only send unattempted arms from a report with the same cap")
    parser.add_argument("--temperature", type=float, choices=TEMPERATURES, default=FIRST_TEMPERATURE,
                        help="One request; check current limits before each subsequent run")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true", help="Explicitly send the candidate options in one request")
    mode.add_argument("--offline-preview", action="store_true", help="Render Python candidates without Groq or dotenv")
    prompt_options = parser.add_mutually_exclusive_group()
    prompt_options.add_argument("--prompt-file", type=Path, help="Exact source prompt, reviewed before calling")
    prompt_options.add_argument("--allow-reconstructed-prompt", action="store_true", help="Explicitly accept the draft instead")
    args = parser.parse_args()
    output = args.output.resolve()
    if output.parent != LOCAL_OUTPUT or output.suffix != ".json" or output.name.endswith("_sample.json") or output.exists():
        parser.error("Use a new ignored JSON file directly in output/")
    if args.live and not (args.prompt_file or args.allow_reconstructed_prompt):
        parser.error("Live mode requires a prompt; no request made")
    if args.offline_preview and args.resume_from:
        parser.error("Offline preview cannot resume a live report")
    try:
        all_cases = {case.review_id: case for case in load_cases(args.fixture, args.comparison)}
        cases = tuple(all_cases[review_id] for review_id in LIVE_REVIEW_IDS)
        baseline = comparison_rows(cases, args.comparison, args.prior_live)
        if args.offline_preview:
            _write_result(output, offline_preview(cases, baseline))
            print("Offline preview: 0 API requests.")
            return 0
        prompt = args.prompt_file.read_text(encoding="utf-8-sig") if args.prompt_file else RECONSTRUCTED_PROMPT
        prompt_source = "provided" if args.prompt_file else "reconstructed"
        prompt_digest = sha256(system_prompt(prompt).encode("utf-8")).hexdigest()
        completed = resume_arms(json.loads(args.resume_from.read_text(encoding="utf-8")), prompt_digest=prompt_digest,
                                prompt_source=prompt_source, baseline=baseline) if args.resume_from else {}
        if args.temperature in completed or (not completed and args.temperature != FIRST_TEMPERATURE):
            raise ValueError("First request must use 0.3; do not repeat attempted settings")
    except (OSError, KeyError, ValueError, TypeError, AttributeError):
        parser.error("Invalid fixture, source prompt or comparison/resume report (including a cap mismatch); no request made")

    from dotenv import load_dotenv

    load_dotenv(LOCAL_OUTPUT.parent / ".env", override=False)
    settings = load_settings()
    if not settings.enabled or settings.model != LIVE_MODEL:
        parser.error("Existing local settings are unavailable or do not select the approved model; no request made")
    try:
        new_arms = asyncio.run(compare_temperatures(cases, prompt=prompt, settings=settings,
                                                    temperatures=(args.temperature,)))
    except ValueError:
        parser.error("Unsafe or oversized payload; no request made")
    merged = completed | {arm["temperature"]: arm for arm in new_arms}
    arms = [merged.get(t, {"temperature": t, "attempts": 0, "http_status": None, "status": "not_requested"})
            for t in TEMPERATURES]
    data = {
        "experiment": "local_only", "source_prompt": prompt_source, "effective_prompt_sha256": prompt_digest,
        "summary_candidate_version": SUMMARY_CANDIDATE_VERSION,
        "top_p": 1.0, "max_completion_tokens": FREEFORM_MAX_COMPLETION_TOKENS,
        "rows": baseline, "arms": arms, "actual_requests": sum(arm["attempts"] for arm in arms),
        "new_requests": sum(arm["attempts"] for arm in new_arms),
    }
    _write_result(output, data)
    print(f"Local A/B: {data['new_requests']} new request(s), {data['actual_requests']} cumulative; "
          f"{sum(arm['status'] == 'format_passed' for arm in arms)} approved batch(es).")
    return 0 if new_arms[0]["status"] == "format_passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
